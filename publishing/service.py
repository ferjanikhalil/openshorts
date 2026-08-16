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
    CREDENTIAL_FATAL, DESTINATION_FATAL, E_QUOTA_EXHAUSTED, ProviderError,
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


async def active_credential(session, group_id, kind: str = "api_key"):
    """Newest usable credential of a kind for a group.

    Rotation is by-insert, so "newest active" is the current one; a credential
    the provider has already 401'd is excluded so the dispatcher stops spending
    attempts on a key that is definitively dead.
    """
    row = (await session.execute(
        select(PublishCredential)
        .where(PublishCredential.publish_group_id == _as_uuid(group_id),
               PublishCredential.kind == kind,
               PublishCredential.active.is_(True),
               PublishCredential.revoked_at.is_(None),
               PublishCredential.invalid_at.is_(None))
        .order_by(PublishCredential.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    return row


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
                        defer_seconds: Optional[int] = None) -> None:
    """Put a claimed attempt back without consuming a try.

    Used when dispatch declines to submit (quota, cooldown, missing media) —
    those are not failures of the post and must not count toward max_attempts.
    """
    attempt.status = state.DEFERRED if defer_seconds else state.PENDING
    attempt.claimed_at = None
    attempt.claimed_by = None
    if defer_seconds:
        attempt.deferred_until = _now() + timedelta(seconds=defer_seconds)
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
    dest.health = "blocked"
    dest.health_detail = err.message[:500]
    await log_event(session, "destination.blocked", message=err.message[:500],
                    destination_id=dest.id, group_id=dest.publish_group_id,
                    request_id=attempt.publish_request_id)


async def _mark_credential_invalid(session, attempt, err, credential_id) -> None:
    """A 401 means every queued post for this group will also fail.

    Marking the credential stops the other 26 posts of the day from each
    rediscovering the same dead key, and gives the admin UI something concrete
    to show.
    """
    cred = None
    if credential_id:
        cred = await session.get(PublishCredential, credential_id)
    if cred is None:
        cred = await active_credential(session, attempt.publish_group_id)
    if cred is None:
        return
    cred.invalid_at = _now()
    cred.invalid_reason = err.message[:500]
    await log_event(session, "credential.invalid", message=err.message[:500],
                    group_id=attempt.publish_group_id,
                    request_id=attempt.publish_request_id,
                    data={"credential_id": str(cred.id),
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

    This is the safety net that replaces polling: the provider has no verified
    status-lookup endpoint, so an attempt whose webhook never arrives would sit
    in ``submitted`` forever and hold the live-attempt slot. Moving it to
    ``unknown`` surfaces it to a human WITHOUT retrying it.

    A submitted attempt carrying a future ``deferred_until`` is a post the
    provider deliberately parked for a later window (a daily-cap 202). Silence
    before that window is expected, not suspicious, so it is left alone until
    the window has passed and the usual timeout has run from there.
    """
    cutoff = _now() - timedelta(seconds=settings.submit_timeout_seconds)
    rows = (await session.execute(
        select(PublishAttempt)
        .where(PublishAttempt.status == state.SUBMITTED,
               PublishAttempt.submitted_at.is_not(None),
               PublishAttempt.submitted_at < cutoff,
               (PublishAttempt.deferred_until.is_(None))
               | (PublishAttempt.deferred_until < cutoff))
        .with_for_update(skip_locked=True)
    )).scalars().all()
    for row in rows:
        row.status = state.UNKNOWN
        row.completed_at = _now()
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
    return len(rows)


async def recover_orphaned_claims(session) -> int:
    """Return attempts claimed by a worker that died mid-dispatch.

    IN_FLIGHT means "claimed, not yet handed to the provider". On a clean
    restart nothing is in flight, so any such row is an orphan and safe to
    re-queue — unlike SUBMITTED, which may correspond to a live post.
    """
    rows = (await session.execute(
        select(PublishAttempt).where(PublishAttempt.status == state.IN_FLIGHT)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    for row in rows:
        row.status = state.PENDING
        row.claimed_at = None
        row.claimed_by = None
    return len(rows)


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
