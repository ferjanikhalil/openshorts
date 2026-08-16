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
    SUBMITTED: {SUCCEEDED, FAILED, UNKNOWN},
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
