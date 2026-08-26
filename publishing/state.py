"""The publishing state machine — pure functions, no I/O, no imports from the app.

Everything here is deliberately dependency-free so it can be unit-tested in CI,
which has no Postgres and no provider credentials. This is where the rules that
decide whether a real post goes out live, so it is the part that most needs to be
testable without a network.

Attempt lifecycle (one row per destination per try):

    pending ──► in_flight ──► submitted ──► succeeded
       │            │             │    └──► failed ──► (retry: new attempt)
       │            │             └───────► unknown        │
       │            └────────────► failed ─────────────────┘
       ├──► deferred ──► pending          (quota / cooldown / schedule)
       ├──► blocked                       (destination disconnected)
       ├──► skipped                       (nothing to do)
       └──► cancelled

``unknown`` is the important one. It means we handed the post to the provider and
never learned the outcome. It is terminal and NEVER auto-retried: a blind retry
on a post that may already be live double-publishes to a real audience. A human
decides.
"""
import hashlib
from datetime import timezone
from typing import Iterable, Optional

# --- Attempt states ---------------------------------------------------------
PENDING = "pending"
IN_FLIGHT = "in_flight"
SUBMITTED = "submitted"
SUCCEEDED = "succeeded"
FAILED = "failed"
DEFERRED = "deferred"
DEAD = "dead"
BLOCKED = "blocked"
UNKNOWN = "unknown"
SKIPPED = "skipped"
CANCELLED = "cancelled"

ATTEMPT_STATES = frozenset({
    PENDING, IN_FLIGHT, SUBMITTED, SUCCEEDED, FAILED, DEFERRED, DEAD, BLOCKED,
    UNKNOWN, SKIPPED, CANCELLED,
})

# States that mean "this attempt is finished, one way or another".
TERMINAL_STATES = frozenset({
    SUCCEEDED, DEAD, BLOCKED, UNKNOWN, SKIPPED, CANCELLED,
})

# States that occupy the live-attempt slot for a (request, destination) pair.
# Mirrors the partial unique index in models.py — the two MUST agree, or the
# database would permit a second in-flight attempt for the same destination.
LIVE_STATES = frozenset({PENDING, IN_FLIGHT, SUBMITTED, SUCCEEDED})

# States a human may resolve manually.
NEEDS_ATTENTION = frozenset({UNKNOWN, DEAD, BLOCKED})

_ALLOWED_TRANSITIONS = {
    PENDING:   {IN_FLIGHT, DEFERRED, CANCELLED, SKIPPED, BLOCKED},
    IN_FLIGHT: {SUBMITTED, SUCCEEDED, FAILED, DEFERRED, BLOCKED, UNKNOWN},
    # BLOCKED belongs here for the same reason it belongs under IN_FLIGHT: the
    # provider can report "this account is not connected / needs re-linking" as
    # the outcome of a post it already accepted, and that arrives AFTER the
    # submit — through a `post.failed` webhook, or through a status poll. Without
    # it `record_failure` raised on the transition, the webhook drain caught the
    # exception and wrote `process_error`, and the completion signal was silently
    # swallowed: the post then aged into `unknown` with a destination still
    # marked healthy, so the next 26 posts of the day each rediscovered the same
    # dead platform token.
    SUBMITTED: {SUCCEEDED, FAILED, UNKNOWN, BLOCKED},
    DEFERRED:  {PENDING, CANCELLED, SKIPPED},
    FAILED:    {DEAD},           # exhausted retries
    # Terminal.
    SUCCEEDED: set(),
    DEAD:      set(),
    BLOCKED:   set(),
    UNKNOWN:   set(),
    SKIPPED:   set(),
    CANCELLED: set(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in _ALLOWED_TRANSITIONS.get(src, set())


def assert_transition(src: str, dst: str) -> None:
    """Raise on an illegal move. Called before every attempt status write.

    Cheap, and it turns "why is this attempt succeeded when it was cancelled"
    into a loud failure at the write site instead of a mystery in the audit log.
    """
    if dst not in ATTEMPT_STATES:
        raise ValueError(f"unknown attempt state: {dst!r}")
    if src == dst:
        return
    if not can_transition(src, dst):
        raise ValueError(f"illegal attempt transition: {src} -> {dst}")


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATES


# --- Request status (DERIVED, never independently written) ------------------
REQ_PENDING = "pending"
REQ_IN_PROGRESS = "in_progress"
REQ_SUCCEEDED = "succeeded"
REQ_PARTIAL = "partial"
REQ_FAILED = "failed"
REQ_DEFERRED = "deferred"
REQ_CANCELLED = "cancelled"


def derive_request_status(attempt_statuses: Iterable[str]) -> str:
    """Compute a request's status purely from its attempts.

    ``partial`` is the state that matters operationally and the reason this is a
    function rather than a column someone writes: publishing one clip to three
    accounts where TikTok fails is NOT a failed request and NOT a successful one.
    Collapsing it either way loses the only fact the operator needs — which
    account still needs attention.

    Precedent: autopilot.progress() derives parent state from children the same
    way.
    """
    statuses = list(attempt_statuses)
    if not statuses:
        return REQ_PENDING

    # Skipped attempts never happened; they must not tip a request to partial.
    effective = [s for s in statuses if s != SKIPPED]
    if not effective:
        return REQ_SUCCEEDED

    if all(s == CANCELLED for s in effective):
        return REQ_CANCELLED

    live = [s for s in effective if s in (PENDING, IN_FLIGHT, SUBMITTED)]
    deferred = [s for s in effective if s == DEFERRED]
    won = [s for s in effective if s == SUCCEEDED]
    lost = [s for s in effective if s in (DEAD, BLOCKED, UNKNOWN, FAILED)]

    if live:
        return REQ_IN_PROGRESS
    if deferred:
        # Nothing in flight but something is waiting on a clock/quota. Still
        # in_progress if part of it already landed — the operator cares that the
        # request is not finished, and 'deferred' reads as "nothing happened yet".
        return REQ_IN_PROGRESS if won else REQ_DEFERRED
    if won and lost:
        return REQ_PARTIAL
    if won:
        return REQ_SUCCEEDED
    if lost:
        return REQ_FAILED
    return REQ_PENDING


def request_is_terminal(status: str) -> bool:
    return status in (REQ_SUCCEEDED, REQ_PARTIAL, REQ_FAILED, REQ_CANCELLED)


# --- Retry policy -----------------------------------------------------------
def backoff_seconds(attempt_number: int, *, base: int = 60, cap: int = 3600,
                    jitter_seed: Optional[str] = None) -> int:
    """Exponential backoff with deterministic jitter.

    Jitter is derived by hashing ``jitter_seed`` (the attempt id) rather than
    drawn randomly, so the delay is reproducible in tests and identical across
    workers that recompute it. Its real job is de-synchronizing retries: when a
    provider outage fails 27 posts at once, un-jittered backoff retries all 27 in
    the same second and re-creates the thundering herd that caused the outage.
    """
    n = max(1, int(attempt_number))
    delay = min(cap, base * (2 ** (n - 1)))
    if jitter_seed:
        h = int(hashlib.sha256(jitter_seed.encode()).hexdigest()[:8], 16)
        # +/- 20%, deterministic per attempt.
        spread = delay * 0.2
        delay = delay - spread + (h % 1000) / 1000.0 * (2 * spread)
    return int(max(1, min(cap, delay)))


def should_retry(attempt_number: int, max_attempts: int, retryable: bool) -> bool:
    return bool(retryable) and attempt_number < max_attempts


# --- Polling policy ---------------------------------------------------------
def _as_utc(value):
    """Treat a naive timestamp as UTC.

    Every column feeding this is ``DateTime(timezone=True)``, so a naive value
    means the row was written by something that dropped the offset. Comparing it
    to an aware ``now`` would raise TypeError inside the reconcile loop, which is
    a worse outcome than assuming the timezone the whole system stores in.
    """
    if value is not None and value.tzinfo is None:  # pragma: no cover
        return value.replace(tzinfo=timezone.utc)
    return value


def poll_is_due(now, *, submitted_at, last_polled_at, deferred_until,
                min_age_seconds: int, interval_seconds: int) -> bool:
    """May this submitted post be asked about yet?

    Pure, and split out for the same reason as ``service.claim_is_recoverable``:
    it is the rule that bounds how much traffic the poller aims at a provider,
    and it has to be verifiable without a database. ``dispatcher`` expresses the
    same three conditions as a SQL ``WHERE`` so ``LIMIT`` selects useful rows,
    then re-checks THIS function before spending a request — so if the two ever
    drift, the disagreement can only skip a poll, never add one.

    Three independent gates, all of which must pass:

    * ``min_age_seconds`` since submit. Polling at t+2s spends a request to be
      told what the submit response already said, and the provider is still
      working. It must also stay well under ``submit_timeout_seconds`` or a post
      would reach ``unknown`` before it was ever asked about.
    * ``interval_seconds`` since the last poll. Reconciliation runs every 60s; a
      post pending for an hour must cost ~12 requests, not 60. ``None`` means
      never polled, which is due.
    * no future ``deferred_until``. On a submitted row that timestamp is a window
      the PROVIDER asked for (a daily-cap 202), so silence before it is expected
      rather than suspicious — the same reading ``confirmation_is_overdue`` takes.
      That function additionally waits out a remote-schedule ``scheduled_for``;
      this one does not, and deliberately so. Asking early about a post the
      provider is holding is merely wasted requests, and the answer ("scheduled")
      is a useful liveness check — whereas answering early with ``unknown`` is
      terminal.
    """
    submitted_at = _as_utc(submitted_at)
    if submitted_at is None:
        # Nothing was handed over that we know of, so there is nothing to ask
        # about — and no clock to measure the floor against.
        return False
    if (now - submitted_at).total_seconds() < max(0, int(min_age_seconds)):
        return False

    deferred_until = _as_utc(deferred_until)
    if deferred_until is not None and deferred_until > now:
        return False

    last_polled_at = _as_utc(last_polled_at)
    if last_polled_at is None:
        return True
    return (now - last_polled_at).total_seconds() >= max(0, int(interval_seconds))


def confirmation_is_overdue(now, *, submitted_at, deferred_until, scheduled_for,
                            timeout_seconds: int) -> bool:
    """Has this submitted post been silent long enough to be called ``unknown``?

    Pure for the same reason as ``poll_is_due``: the verdict it feeds is
    ``unknown``, which is TERMINAL and never auto-retried, so the rule that
    produces it has to be checkable without a database. ``sweep_stale_submitted``
    expresses it as a SQL ``WHERE`` to keep the scan index-friendly and then
    re-checks this function per row, so drift between the two can only spare a
    post, never condemn one.

    The timeout does not run from the submit — it runs from the last moment the
    provider was expected to still be holding the post:

    * ``deferred_until``, when the PROVIDER asked for a later window (a daily-cap
      202). Silence before it is expected rather than suspicious.
    * ``scheduled_for`` on the request, which is the case ``deferred_until``
      cannot cover. Under remote-schedule hand-over the submit goes out
      immediately carrying ``scheduledFor`` and the provider holds the clock;
      ``dispatcher.promote_remote_schedules`` clears ``deferred_until`` precisely
      because the local clock is no longer the one in charge. Reading only
      ``deferred_until`` would therefore condemn every hand-over — a rhythm slot
      hours out against a 30-minute timeout — before the post was ever due to go
      live, destroying the capability the hand-over exists to provide.

    A confirmation is only late once the latest of those has passed AND the usual
    timeout has run from there.
    """
    submitted_at = _as_utc(submitted_at)
    if submitted_at is None:
        # Nothing was handed over that we know of, so there is no silence to
        # measure and nothing to condemn.
        return False
    quiet_until = max(t for t in (submitted_at, _as_utc(deferred_until),
                                  _as_utc(scheduled_for)) if t is not None)
    return (now - quiet_until).total_seconds() >= max(0, int(timeout_seconds))


# --- Idempotency ------------------------------------------------------------
def content_fingerprint(job_id: str, clip_index: int,
                        size_bytes: Optional[int] = None,
                        mtime: Optional[float] = None) -> str:
    """Stable identity for the CONTENT of a clip at a point in time.

    Clip filenames mutate on every post-processing operation (``subtitled_``,
    ``hook_``, ``translated_``, ``edited_`` prefixes) and ``video_url`` is
    rewritten in place, so a filename or URL is not an identity. ``(job_id,
    clip_index)`` identifies the slot; size+mtime distinguish a re-styled clip
    from the one already uploaded, which is what stops a stale provider media
    ref from being reused for different bytes.
    """
    parts = [str(job_id), str(clip_index)]
    if size_bytes is not None:
        parts.append(str(size_bytes))
    if mtime is not None:
        parts.append(str(int(mtime)))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def derive_idempotency_key(job_id: str, clip_index: int,
                           destination_ids: Iterable[str],
                           scheduled_for: Optional[str] = None) -> str:
    """Server-side dedupe key when the caller supplies none.

    Keyed over the SORTED destination set, so publishing the same clip to a
    different set of accounts is correctly a different request, while a
    double-clicked button with the same set collapses into one.
    """
    dests = ",".join(sorted(str(d) for d in destination_ids))
    raw = f"{job_id}|{clip_index}|{dests}|{scheduled_for or ''}"
    return "auto_" + hashlib.sha256(raw.encode()).hexdigest()[:40]
