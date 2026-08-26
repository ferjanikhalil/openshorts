"""Core publishing service: expand destinations, create work, record outcomes.

This is the layer that turns a user intent ("publish clip 4 to these accounts")
into attempt rows, and turns provider outcomes back into state. It holds every
rule that must not be duplicated elsewhere:

  * A request ALWAYS expands to a flat list of destinations. Single-account,
    hand-picked multi-account and whole-group publishing differ only in how that
    list is produced — after expansion there is one code path. ``mode`` is
    recorded for the UI and never branches behaviour.
  * An attempt is created per destination, never per group.
  * A retry is a NEW attempt row, so history is append-only and "how many times
    did we try TikTok 2" is answerable.
  * The request's status is recomputed from its attempts on every change, never
    written independently.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import state
from .config import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, settings
from .errors import (
    CREDENTIAL_FATAL, DESTINATION_FATAL, E_ACCOUNT_AUTH, E_QUOTA_EXHAUSTED,
    ProviderError,
)
from .models import (
    PublishAttempt, PublishCredential, PublishDestination, PublishEvent,
    PublishRequest,
)


def _now():
    return datetime.now(timezone.utc)


def _as_uuid(value):
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# --- Audit ------------------------------------------------------------------
async def log_event(session, kind: str, *, message: str = "",
                    request_id=None, attempt_id=None, destination_id=None,
                    group_id=None, actor: Optional[str] = None,
                    data: Optional[dict] = None) -> None:
    """Append to the audit log. Never raises into the caller's transaction path
    for a formatting problem — losing an audit line must not fail a publish."""
    try:
        session.add(PublishEvent(
            kind=kind, message=message or "", actor=actor,
            publish_request_id=request_id, publish_attempt_id=attempt_id,
            publish_destination_id=destination_id, publish_group_id=group_id,
            data=_jsonable(data) if data else None,
        ))
    except Exception as e:  # pragma: no cover - defensive
        print(f"⚠️  Publishing: failed to record event {kind}: {e}")


def _jsonable(value):
    """Make provider payloads safe for a JSONB column (datetimes, sets, ...)."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --- Destination expansion --------------------------------------------------
async def expand_destinations(session, *, destination_ids: Iterable = (),
                              group_ids: Iterable = (),
                              platforms: Optional[Iterable] = None
                              ) -> List[PublishDestination]:
    """Resolve any mix of explicit destinations and groups into ONE list.

    This function is the reason "batch publishing" and "individual publishing"
    are not separate systems. A group contributes its enabled destinations; an
    explicit id contributes itself; the union is de-duplicated so naming both a
    group and one of its members cannot double-post.

    Disabled or unhealthy destinations are dropped here rather than failing
    later — the caller sees exactly what will be attempted.
    """
    dest_ids = {_as_uuid(d) for d in (destination_ids or [])}
    grp_ids = {_as_uuid(g) for g in (group_ids or [])}

    found = {}
    if dest_ids:
        rows = (await session.execute(
            select(PublishDestination).where(PublishDestination.id.in_(dest_ids))
        )).scalars().all()
        for row in rows:
            found[row.id] = row
    if grp_ids:
        rows = (await session.execute(
            select(PublishDestination).where(
                PublishDestination.publish_group_id.in_(grp_ids))
        )).scalars().all()
        for row in rows:
            found.setdefault(row.id, row)

    out = []
    wanted = {str(p).lower() for p in platforms} if platforms else None
    for row in found.values():
        if not row.enabled:
            continue
        # 'unverified' is allowed through: with no account-listing endpoint the
        # first real publish IS the verification. Only a proven-bad destination
        # is skipped.
        if row.health in ("blocked", "disconnected"):
            continue
        if wanted and row.platform.lower() not in wanted:
            continue
        out.append(row)
    out.sort(key=lambda d: (d.platform, str(d.id)))
    return out


_UNSET = object()


async def active_credential(session, group_id, kind: str = "api_key",
                            include_invalid: bool = False,
                            slot=_UNSET):
    """Newest usable credential of a kind for a group.

    Rotation is by-insert, so "newest active" is the current one; a credential
    the provider has already 401'd is excluded so the dispatcher stops spending
    attempts on a key that is definitively dead.

    ``include_invalid=True`` is for the two callers that must see a rejected key
    rather than pretend there is none: the admin view (which has to say "this key
    was rejected, here is why" instead of "no key stored") and the verify
    endpoint (whose whole job is to clear the flag once the key works again).
    Dispatch must never pass it — that is what the exclusion is for.

    ``slot`` selects ONE provider account inside the group. Omitted means "any
    slot", which is the historical behaviour and what group-wide callers want;
    ``slot=None`` means specifically the default (unslotted) credential, which is
    not the same question. Keeping those distinct is what stops a group-wide
    lookup from silently answering with a slotted key it has no business using —
    and vice versa. Use ``credential_for_destination`` for dispatch.
    """
    stmt = (
        select(PublishCredential)
        .where(PublishCredential.publish_group_id == _as_uuid(group_id),
               PublishCredential.kind == kind,
               PublishCredential.active.is_(True),
               PublishCredential.revoked_at.is_(None))
        .order_by(PublishCredential.created_at.desc())
        .limit(1)
    )
    if slot is not _UNSET:
        stmt = (stmt.where(PublishCredential.credential_slot.is_(None))
                if slot in (None, "") else
                stmt.where(PublishCredential.credential_slot == slot))
    if not include_invalid:
        stmt = stmt.where(PublishCredential.invalid_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def active_credentials(session, group_id, kind: str = "api_key",
                             include_invalid: bool = False) -> list:
    """Every current credential of a kind for a group — one per slot.

    "Current" is still newest-active-per-slot, because rotation is by-insert and
    a superseded row stays behind for the audit trail. The admin view needs the
    whole set: a group holding two provider accounts has two keys that can fail
    independently, and showing only one of them hides the exact key a destination
    is stuck on.

    Not for dispatch. Dispatch resolves ONE key through the destination — see
    ``credential_for_destination``.
    """
    stmt = (
        select(PublishCredential)
        .where(PublishCredential.publish_group_id == _as_uuid(group_id),
               PublishCredential.kind == kind,
               PublishCredential.active.is_(True),
               PublishCredential.revoked_at.is_(None))
        .order_by(PublishCredential.created_at.desc())
    )
    if not include_invalid:
        stmt = stmt.where(PublishCredential.invalid_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()

    newest_per_slot, out = set(), []
    for row in rows:  # already newest-first
        slot = row.credential_slot or ""
        if slot in newest_per_slot:
            continue
        newest_per_slot.add(slot)
        out.append(row)
    # Default first, then slots alphabetically: a stable order the UI can render
    # without sorting, and one where the group's primary key stays on top.
    out.sort(key=lambda c: (c.credential_slot is not None,
                            c.credential_slot or ""))
    return out


async def credential_for_destination(session, destination, kind: str = "api_key",
                                     include_invalid: bool = False):
    """The credential a specific destination publishes through.

    Resolution is slot-then-default: a destination naming a slot uses that slot's
    key, and anything unslotted uses the group default. The fallback is
    deliberately one-directional — a slotted destination does NOT fall back to
    the default key.

    That looks unhelpful until you consider what the fallback would do on the
    shape this exists for. A Zernio group holds two accounts because neither can
    connect all three platforms; account A has TikTok and YouTube, account B has
    Instagram. Falling back would submit the Instagram post with account A's key,
    where the provider answers that no such account is connected — after the
    submit, having spent an attempt, and with an error that reads like a
    disconnected social account rather than a mapping mistake. Returning nothing
    parks the attempt with "no credential for slot 'zernio-b'", which says
    exactly what the operator has to fix.
    """
    slot = (getattr(destination, "credential_slot", None) or "").strip()
    if slot:
        return await active_credential(session, destination.publish_group_id,
                                       kind, include_invalid, slot=slot)
    return await active_credential(session, destination.publish_group_id,
                                   kind, include_invalid, slot=None)


# --- Request creation -------------------------------------------------------
async def create_request(session, *, job_id: str, clip_index: int,
                         destinations: List[PublishDestination],
                         payload: Optional[dict] = None,
                         mode: str = "multi",
                         scheduled_for: Optional[datetime] = None,
                         content_fingerprint: Optional[str] = None,
                         idempotency_key: Optional[str] = None,
                         user_id=None, actor: Optional[str] = None,
                         return_created: bool = False
                         ) -> PublishRequest:
    """Create a request plus one pending attempt per destination.

    Idempotent on ``idempotency_key``: a repeat call returns the original
    request instead of creating a second one. The SELECT below is the fast path,
    but it is not the guarantee — two concurrent callers would both pass it. The
    UNIQUE column is the guarantee, and losing that race is handled rather than
    raised, because the cost of getting this wrong is a duplicate post on a real
    account.

    ``return_created`` is for planners that themselves report "newly queued vs
    already had a request": when true, the return is wrapped as ``(request,
    was_created)`` so the caller can put a reused request under *existing*
    instead of *created* and stop advertising work that was not done.
    """
    key = idempotency_key or state.derive_idempotency_key(
        job_id, clip_index, [str(d.id) for d in destinations],
        scheduled_for.isoformat() if scheduled_for else None)

    existing = await _request_by_key(session, key)
    if existing is not None:
        return (existing, False) if return_created else existing

    req = PublishRequest(
        user_id=user_id, job_id=job_id, clip_index=clip_index,
        content_fingerprint=content_fingerprint, mode=mode,
        payload=_jsonable(payload or {}), scheduled_for=scheduled_for,
        status=state.REQ_PENDING, idempotency_key=key, created_by=actor,
    )
    try:
        # Savepoint: a UNIQUE violation here must not poison the outer
        # transaction, because we intend to keep using it.
        async with session.begin_nested():
            session.add(req)
            await session.flush()
    except IntegrityError:
        # The concurrent caller won. Its request is the canonical one, and its
        # attempts were created in the same transaction, so returning it is
        # complete — not a partially-built duplicate.
        winner = await _request_by_key(session, key)
        if winner is None:
            raise
        return (winner, False) if return_created else winner

    for dest in destinations:
        session.add(PublishAttempt(
            publish_request_id=req.id,
            publish_destination_id=dest.id,
            publish_group_id=dest.publish_group_id,
            provider=dest.provider,
            platform=dest.platform,
            attempt_number=1,
            status=state.PENDING,
            deferred_until=scheduled_for,
        ))

    await log_event(session, "request.created",
                    message=f"{len(destinations)} destination(s)",
                    request_id=req.id, actor=actor,
                    data={"job_id": job_id, "clip_index": clip_index,
                          "mode": mode,
                          "destinations": [str(d.id) for d in destinations]})
    await session.flush()
    return (req, True) if return_created else req


async def _request_by_key(session, key: str) -> Optional[PublishRequest]:
    return (await session.execute(
        select(PublishRequest).where(PublishRequest.idempotency_key == key)
    )).scalar_one_or_none()


# --- Claiming ---------------------------------------------------------------
async def claim_due_attempts(session, worker_id: str, limit: int = 10
                             ) -> List[PublishAttempt]:
    """Atomically claim attempts that are ready to run.

    ``FOR UPDATE SKIP LOCKED`` is what lets several workers (and several app
    replicas) share the queue with no broker — the same trick
    ``cloud/metering.py`` already uses. Without SKIP LOCKED, two workers would
    read the same pending row and submit the same post twice.
    """
    now = _now()
    stmt = (
        select(PublishAttempt)
        .where(PublishAttempt.status.in_([state.PENDING, state.DEFERRED]))
        .where((PublishAttempt.deferred_until.is_(None))
               | (PublishAttempt.deferred_until <= now))
        .order_by(PublishAttempt.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    claimed = []
    for row in rows:
        row.status = state.IN_FLIGHT
        row.claimed_at = now
        row.claimed_by = worker_id
        claimed.append(row)
    if claimed:
        await session.flush()
    return claimed


async def release_claim(session, attempt: PublishAttempt,
                        defer_seconds: Optional[int] = None,
                        reason: Optional[str] = None,
                        code: Optional[str] = None) -> None:
    """Put a claimed attempt back without consuming a try.

    Used when dispatch declines to submit (quota, cooldown, missing media) —
    those are not failures of the post and must not count toward max_attempts.

    ``reason`` matters more than it looks. Because this path consumes no try, it
    has no natural end: an attempt can be re-parked every 15 minutes forever. If
    it also records nothing, the board shows only "Scheduled · next 12:38" and
    the operator watches the time march forward with no way to learn why —
    exactly what happened on 2026-08-17. Any decline a human might have to fix
    must pass a reason so it lands on the row.
    """
    attempt.status = state.DEFERRED if defer_seconds else state.PENDING
    attempt.claimed_at = None
    attempt.claimed_by = None
    if defer_seconds:
        attempt.deferred_until = _now() + timedelta(seconds=defer_seconds)
    if reason is not None:
        attempt.error_code = code
        attempt.error_message = reason[:2000]
    await session.flush()


# --- Outcome recording ------------------------------------------------------
async def record_success(session, attempt: PublishAttempt, result,
                         credential_id=None) -> None:
    target = (state.SUCCEEDED if result.status == "succeeded"
              else state.SUBMITTED)
    state.assert_transition(attempt.status, target)
    attempt.status = target
    attempt.provider_post_ref = result.provider_post_ref
    attempt.provider_native_post_ref = result.provider_native_post_ref
    attempt.permalink = result.permalink
    attempt.provider_response = _jsonable(result.raw)
    attempt.quota_snapshot = _jsonable(result.quota)
    attempt.submitted_at = _now()
    attempt.publish_credential_id = credential_id
    # Any error text on this row described a hold that has now cleared (a quota
    # wait, a missing key). Leaving it would print a red line under a post that
    # went out fine.
    attempt.error_code = None
    attempt.error_message = None
    if target == state.SUCCEEDED:
        attempt.completed_at = _now()
    elif result.defer_seconds:
        # The provider accepted the post but parked it for a later window, so
        # nothing can confirm it before then. Recording that window keeps the
        # stale sweeper from calling the silence suspicious while it is still
        # normal. This does NOT re-queue the post: claim_due_attempts only ever
        # claims pending/deferred rows, and this one is submitted.
        attempt.deferred_until = _now() + timedelta(
            seconds=max(1, int(result.defer_seconds)))
    await _apply_quota(session, attempt, result.quota)
    await log_event(session, f"attempt.{target}",
                    message=result.provider_post_ref or "",
                    request_id=attempt.publish_request_id,
                    attempt_id=attempt.id,
                    destination_id=attempt.publish_destination_id,
                    group_id=attempt.publish_group_id)
    await refresh_request_status(session, attempt.publish_request_id)


async def record_deferral(session, attempt: PublishAttempt, seconds: int,
                          reason: str, quota: Optional[dict] = None) -> None:
    """Park an attempt on a clock without consuming a try."""
    state.assert_transition(attempt.status, state.DEFERRED)
    attempt.status = state.DEFERRED
    attempt.deferred_until = _now() + timedelta(seconds=max(1, int(seconds)))
    attempt.claimed_at = None
    attempt.claimed_by = None
    attempt.error_code = None
    attempt.error_message = reason
    if quota:
        attempt.quota_snapshot = _jsonable(quota)
        await _apply_quota(session, attempt, quota)
    await log_event(session, "attempt.deferred", message=reason,
                    request_id=attempt.publish_request_id,
                    attempt_id=attempt.id,
                    destination_id=attempt.publish_destination_id,
                    group_id=attempt.publish_group_id,
                    data={"until": attempt.deferred_until})
    await refresh_request_status(session, attempt.publish_request_id)


async def record_failure(session, attempt: PublishAttempt, err: ProviderError,
                         credential_id=None) -> Optional[PublishAttempt]:
    """Record a failed attempt and create the retry, if one is warranted.

    Returns the new attempt row when a retry was scheduled, else None.
    """
    attempt.error_code = err.code
    attempt.error_message = err.message[:2000] if err.message else err.code
    attempt.provider_response = _jsonable(err.response)
    attempt.completed_at = _now()
    attempt.publish_credential_id = credential_id
    if err.provider_post_ref and not attempt.provider_post_ref:
        attempt.provider_post_ref = err.provider_post_ref

    if err.is_ambiguous:
        # The post may be live. Terminal and never auto-retried — a human
        # decides, because a blind retry double-publishes to a real audience.
        state.assert_transition(attempt.status, state.UNKNOWN)
        attempt.status = state.UNKNOWN
        await log_event(session, "attempt.unknown", message=err.message,
                        request_id=attempt.publish_request_id,
                        attempt_id=attempt.id,
                        destination_id=attempt.publish_destination_id,
                        group_id=attempt.publish_group_id)
        await refresh_request_status(session, attempt.publish_request_id)
        return None

    if err.code in DESTINATION_FATAL:
        state.assert_transition(attempt.status, state.BLOCKED)
        attempt.status = state.BLOCKED
        await _mark_destination_unhealthy(session, attempt, err)
        await refresh_request_status(session, attempt.publish_request_id)
        return None

    if err.code in CREDENTIAL_FATAL:
        state.assert_transition(attempt.status, state.FAILED)
        attempt.status = state.FAILED
        await _mark_credential_invalid(session, attempt, err, credential_id)
        attempt.status = state.DEAD
        await refresh_request_status(session, attempt.publish_request_id)
        return None

    state.assert_transition(attempt.status, state.FAILED)
    attempt.status = state.FAILED

    if err.is_capacity:
        # Capacity limits do not consume a try: the post was never evaluated.
        # Charging them would let one throttled hour exhaust max_attempts.
        retry = await _create_retry(session, attempt,
                                    same_attempt_number=True,
                                    defer_seconds=err.defer_seconds or 3600)
        if err.code == E_QUOTA_EXHAUSTED:
            await _apply_quota(session, attempt,
                               {"remaining": 0,
                                "reset_at": _now() + timedelta(
                                    seconds=err.defer_seconds or 3600)})
    elif state.should_retry(attempt.attempt_number, settings.max_attempts,
                            err.retryable):
        delay = state.backoff_seconds(
            attempt.attempt_number, base=BACKOFF_BASE_SECONDS,
            cap=BACKOFF_CAP_SECONDS, jitter_seed=str(attempt.id))
        retry = await _create_retry(session, attempt, defer_seconds=delay)
    else:
        state.assert_transition(attempt.status, state.DEAD)
        attempt.status = state.DEAD
        retry = None

    await log_event(session, f"attempt.{attempt.status}",
                    message=f"{err.code}: {err.message}"[:500],
                    request_id=attempt.publish_request_id,
                    attempt_id=attempt.id,
                    destination_id=attempt.publish_destination_id,
                    group_id=attempt.publish_group_id,
                    data={"retry_scheduled": bool(retry)})
    await refresh_request_status(session, attempt.publish_request_id)
    return retry


async def _create_retry(session, attempt: PublishAttempt, *,
                        defer_seconds: int,
                        same_attempt_number: bool = False) -> PublishAttempt:
    """Append a fresh attempt row for the same (request, destination).

    Safe against the live-attempt unique index because the previous row is
    already FAILED by the time this runs — failed rows are outside the partial
    index, so exactly one live attempt per destination remains guaranteed.
    """
    nxt = PublishAttempt(
        publish_request_id=attempt.publish_request_id,
        publish_destination_id=attempt.publish_destination_id,
        publish_group_id=attempt.publish_group_id,
        provider=attempt.provider,
        platform=attempt.platform,
        attempt_number=attempt.attempt_number + (0 if same_attempt_number else 1),
        status=state.DEFERRED,
        deferred_until=_now() + timedelta(seconds=max(1, int(defer_seconds))),
        publish_media_id=attempt.publish_media_id,
    )
    session.add(nxt)
    await session.flush()
    return nxt


async def _mark_destination_unhealthy(session, attempt, err) -> None:
    dest = await session.get(PublishDestination, attempt.publish_destination_id)
    if dest is None:
        return
    # Two different fixes, so two different words. 'blocked' means the reference
    # is wrong or not permitted (check the account id); 'disconnected' means the
    # link to the platform lapsed (re-link the account at the provider) — the
    # same state a profile.disconnected webhook produces. Both are cleared with
    # "reset health" on the account once the operator has fixed it.
    disconnected = err.code == E_ACCOUNT_AUTH
    dest.health = "disconnected" if disconnected else "blocked"
    dest.health_detail = err.message[:500]
    await log_event(session,
                    "destination.disconnected" if disconnected
                    else "destination.blocked",
                    message=err.message[:500],
                    destination_id=dest.id, group_id=dest.publish_group_id,
                    request_id=attempt.publish_request_id)


async def _mark_credential_invalid(session, attempt, err, credential_id) -> None:
    """A 401 means every queued post using THAT key will also fail.

    Marking the credential stops the other 26 posts of the day from each
    rediscovering the same dead key, and gives the admin UI something concrete
    to show.

    Which key gets marked is the whole risk here. ``credential_id`` is the one
    dispatch actually signed with and is always passed on the live path; the
    fallback exists for callers that only have the attempt. That fallback must
    resolve through the DESTINATION, not the group: a group can now hold several
    provider accounts, and a group-wide lookup would answer with whichever key
    was newest — so one dead Zernio account would disable the healthy one and
    take out every platform instead of the one that actually broke.
    """
    cred = None
    if credential_id:
        cred = await session.get(PublishCredential, credential_id)
    if cred is None:
        dest = await session.get(PublishDestination,
                                 attempt.publish_destination_id)
        if dest is not None:
            cred = await credential_for_destination(session, dest)
    if cred is None:
        return
    cred.invalid_at = _now()
    cred.invalid_reason = err.message[:500]
    await log_event(session, "credential.invalid", message=err.message[:500],
                    group_id=attempt.publish_group_id,
                    request_id=attempt.publish_request_id,
                    data={"credential_id": str(cred.id),
                          "slot": cred.credential_slot,
                          "last4": cred.last4})


async def _apply_quota(session, attempt: PublishAttempt,
                       quota: Optional[dict]) -> None:
    """Cache the provider's quota view on the destination row."""
    if not quota:
        return
    dest = await session.get(PublishDestination, attempt.publish_destination_id)
    if dest is None:
        return
    if quota.get("limit") is not None:
        dest.quota_limit = int(quota["limit"])
    if quota.get("remaining") is not None:
        dest.quota_remaining = int(quota["remaining"])
    reset = quota.get("reset_at")
    if isinstance(reset, str):
        try:
            reset = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        except Exception:
            reset = None
    if isinstance(reset, datetime):
        dest.quota_reset_at = reset if reset.tzinfo else reset.replace(
            tzinfo=timezone.utc)


# --- Derived request status -------------------------------------------------
async def refresh_request_status(session, request_id) -> str:
    """Recompute and cache the request status from its attempts.

    Only the LATEST attempt per destination counts: a destination that failed
    twice and then succeeded is a success, and counting the historical rows would
    report a permanently partial request.
    """
    rows = (await session.execute(
        select(PublishAttempt.publish_destination_id, PublishAttempt.status,
               PublishAttempt.attempt_number, PublishAttempt.created_at)
        .where(PublishAttempt.publish_request_id == _as_uuid(request_id))
        .order_by(PublishAttempt.created_at.asc())
    )).all()

    latest = {}
    for dest_id, status_, _num, _created in rows:
        latest[dest_id] = status_

    derived = state.derive_request_status(latest.values())
    req = await session.get(PublishRequest, _as_uuid(request_id))
    if req is not None and req.status != derived:
        req.status = derived
        if state.request_is_terminal(derived):
            req.completed_at = _now()
    return derived


async def cancel_request(session, request_id, actor: Optional[str] = None
                         ) -> dict:
    """Cancel whatever has not yet reached the provider.

    Deliberately partial: an attempt already submitted cannot be recalled (the
    provider exposes no working cancel endpoint), so cancelling reports exactly
    what it could and could not stop rather than implying a clean rollback.
    """
    rows = (await session.execute(
        select(PublishAttempt)
        .where(PublishAttempt.publish_request_id == _as_uuid(request_id))
        .with_for_update()
    )).scalars().all()

    cancelled, uncancellable = 0, 0
    for row in rows:
        if row.status in (state.PENDING, state.DEFERRED):
            row.status = state.CANCELLED
            row.completed_at = _now()
            cancelled += 1
        elif row.status in (state.IN_FLIGHT, state.SUBMITTED):
            uncancellable += 1
    await log_event(session, "request.cancelled",
                    message=f"cancelled {cancelled}, in-flight {uncancellable}",
                    request_id=_as_uuid(request_id), actor=actor)
    status = await refresh_request_status(session, request_id)
    return {"cancelled": cancelled, "already_in_flight": uncancellable,
            "status": status}


async def retry_attempt(session, attempt_id, actor: Optional[str] = None,
                        force: bool = False) -> dict:
    """Human-triggered retry of a terminal attempt.

    ``force`` is required for an ``unknown`` attempt and exists to make the risk
    explicit: that post may already be live, so retrying can double-publish. The
    system will never make that choice on its own.
    """
    attempt = await session.get(PublishAttempt, _as_uuid(attempt_id))
    if attempt is None:
        return {"ok": False, "reason": "not found"}
    if attempt.status in (state.PENDING, state.IN_FLIGHT, state.SUBMITTED,
                          state.DEFERRED):
        return {"ok": False, "reason": f"attempt is still {attempt.status}"}
    if attempt.status == state.SUCCEEDED:
        return {"ok": False, "reason": "attempt already succeeded"}
    if attempt.status == state.UNKNOWN and not force:
        return {"ok": False,
                "reason": ("this post may already be live; retrying could "
                           "publish it twice. Confirm on the platform, then "
                           "retry with force=true.")}

    live = (await session.execute(
        select(func.count()).select_from(PublishAttempt).where(
            PublishAttempt.publish_request_id == attempt.publish_request_id,
            PublishAttempt.publish_destination_id
            == attempt.publish_destination_id,
            PublishAttempt.status.in_(list(state.LIVE_STATES)))
    )).scalar_one()
    if live:
        return {"ok": False, "reason": "a live attempt already exists"}

    retry = PublishAttempt(
        publish_request_id=attempt.publish_request_id,
        publish_destination_id=attempt.publish_destination_id,
        publish_group_id=attempt.publish_group_id,
        provider=attempt.provider, platform=attempt.platform,
        attempt_number=attempt.attempt_number + 1,
        status=state.PENDING,
    )
    session.add(retry)
    await session.flush()
    await log_event(session, "attempt.manual_retry",
                    message=f"from {attempt.status}"
                            + (" (forced)" if force else ""),
                    request_id=attempt.publish_request_id,
                    attempt_id=retry.id,
                    destination_id=attempt.publish_destination_id,
                    group_id=attempt.publish_group_id, actor=actor)
    await refresh_request_status(session, attempt.publish_request_id)
    return {"ok": True, "attempt_id": str(retry.id)}


async def sweep_stale_submitted(session) -> int:
    """Move long-submitted attempts to ``unknown``.

    This is the safety net for a provider that cannot be asked: with no verified
    status-lookup endpoint, an attempt whose webhook never arrives would sit in
    ``submitted`` forever and hold the live-attempt slot. Moving it to ``unknown``
    surfaces it to a human WITHOUT retrying it.

    Which makes a false positive expensive: ``unknown`` is terminal and never
    auto-retried, so condemning a post the provider is still legitimately holding
    strands a clip that was going to publish fine. Two timestamps say "still
    holding", and both must be waited out — ``deferred_until`` (a window the
    provider asked for) and the request's ``scheduled_for`` (a schedule the
    provider agreed to keep, where ``deferred_until`` has been cleared by
    ``dispatcher.promote_remote_schedules`` because the local clock is no longer
    in charge). ``state.confirmation_is_overdue`` is the authority on both; the
    SQL below is an index-friendly pre-filter that it re-checks per row.
    """
    cutoff = _now() - timedelta(seconds=settings.submit_timeout_seconds)
    rows = (await session.execute(
        select(PublishAttempt, PublishRequest.scheduled_for)
        .join(PublishRequest,
              PublishRequest.id == PublishAttempt.publish_request_id)
        .where(PublishAttempt.status == state.SUBMITTED,
               PublishAttempt.submitted_at.is_not(None),
               PublishAttempt.submitted_at < cutoff,
               (PublishAttempt.deferred_until.is_(None))
               | (PublishAttempt.deferred_until < cutoff),
               (PublishRequest.scheduled_for.is_(None))
               | (PublishRequest.scheduled_for < cutoff))
        # Only the attempt is locked: the request is joined to read one column,
        # and locking it would contend with every other pass that touches the
        # same request (refresh_request_status, dispatch, poll). The schedule
        # comes back in this query rather than a per-row load, so there is no
        # N+1 and no chance of reading a stale identity-mapped request.
        .with_for_update(skip_locked=True, of=PublishAttempt)
    )).all()
    now = _now()
    swept = 0
    for row, scheduled_for in rows:
        if not state.confirmation_is_overdue(
                now, submitted_at=row.submitted_at,
                deferred_until=row.deferred_until,
                scheduled_for=scheduled_for,
                timeout_seconds=settings.submit_timeout_seconds):
            continue
        row.status = state.UNKNOWN
        row.completed_at = now
        row.error_code = "no_confirmation"
        row.error_message = (
            "No provider confirmation within "
            f"{settings.submit_timeout_seconds}s. The post may or may not be "
            "live — check the account before retrying.")
        await log_event(session, "attempt.unknown",
                        message="submit confirmation timeout",
                        request_id=row.publish_request_id, attempt_id=row.id,
                        destination_id=row.publish_destination_id,
                        group_id=row.publish_group_id)
        await refresh_request_status(session, row.publish_request_id)
        swept += 1
    return swept


def claim_is_recoverable(claimed_at, now, min_age_seconds: int) -> bool:
    """Is this ``in_flight`` claim old enough to be treated as abandoned?

    Split out as a pure function on purpose. It is the rule that decides whether
    a row a live worker may still be holding gets handed to a second worker, so
    it has to be testable without a database — see
    ``tests/test_publishing_tick.py::TestOrphanRecoveryIsAgeBounded``.

    A claim with no ``claimed_at`` is recoverable immediately: every real claim
    stamps it (``claim_due_attempts``), so a NULL means the row was left
    ``in_flight`` by something that never held a proper claim, and no worker is
    coming back for it.
    """
    if claimed_at is None:
        return True
    if claimed_at.tzinfo is None:  # pragma: no cover - column is tz-aware
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return (now - claimed_at).total_seconds() >= min_age_seconds


async def recover_orphaned_claims(session, min_age_seconds: Optional[int] = None
                                  ) -> int:
    """Return attempts claimed by a worker that died mid-dispatch.

    IN_FLIGHT means "claimed, not yet handed to the provider", so unlike
    SUBMITTED — which may correspond to a live post — such a row is safe to
    re-queue *if nobody is still working on it*.

    That last condition used to be free: with one long-lived process, boot meant
    nothing was in flight. It is not free any more. The app host and the always-on
    publisher (deploy/publisher/) share this queue, and a publisher that runs as a
    scheduled tick boots often — so a recovery pass regularly runs while the other
    process is mid-batch. Re-queuing a claim someone is still working through is
    how one clip becomes two posts.

    So recovery only takes claims that have gone quiet for
    ``settings.orphan_claim_min_age_seconds``. The age bound reduces collisions;
    what makes an early recovery merely wasteful rather than destructive is the
    claim-ownership check in ``dispatcher.dispatch_attempt``.
    """
    if min_age_seconds is None:
        min_age_seconds = settings.orphan_claim_min_age_seconds
    rows = (await session.execute(
        select(PublishAttempt).where(PublishAttempt.status == state.IN_FLIGHT)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    now = _now()
    recovered = 0
    for row in rows:
        if not claim_is_recoverable(row.claimed_at, now, min_age_seconds):
            continue
        row.status = state.PENDING
        row.claimed_at = None
        row.claimed_by = None
        recovered += 1
    return recovered


async def group_summary(session, group_id) -> dict:
    """Counts for the admin dashboard."""
    gid = _as_uuid(group_id)
    dests = (await session.execute(
        select(PublishDestination).where(
            PublishDestination.publish_group_id == gid)
    )).scalars().all()
    counts = dict((await session.execute(
        select(PublishAttempt.status, func.count())
        .where(PublishAttempt.publish_group_id == gid)
        .group_by(PublishAttempt.status)
    )).all())
    return {
        "destinations": len(dests),
        "enabled_destinations": sum(1 for d in dests if d.enabled),
        "unhealthy_destinations": sum(
            1 for d in dests if d.health in ("blocked", "disconnected")),
        "attempts": counts,
        "needs_attention": sum(counts.get(s, 0) for s in state.NEEDS_ATTENTION),
    }
