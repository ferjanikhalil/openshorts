"""Taking one claimed attempt and actually publishing it.

The order of checks here is the whole design. Each one is cheaper than the next,
and each avoids spending something scarce on work that cannot succeed:

  0. claim still ours?      -> free; two processes share this queue, so a claim
                              can be taken over between the transaction that
                              made it and this one. Submitting a post another
                              worker already submitted is the one mistake here
                              with no undo.
  1. clip resolvable?      -> free; a deleted clip can never publish. A
                              publisher-only instance holds no clips at all and
                              answers this from the media table instead — see
                              ``_staged_info``, and note that "no clips in this
                              process" must never be read as "clip deleted".
  2. destination healthy?  -> free
  3. cooldown elapsed?     -> free; the provider already told us to wait
  4. quota remaining?      -> free; 3 posts/day against a cap of 5 leaves a
                              headroom of 2, and retries eat it. Submitting into
                              a known-exhausted quota wastes the attempt AND
                              produces a 202 we then have to reconcile.
  5. size within limits?   -> free; the tightest platform ceiling governs
  6. media ref cached?     -> one upload serves the whole fan-out
  7. submit                -> the only step that touches a real audience

Concurrency is bounded twice: globally, and per credential. The per-credential
bound matters because quotas are per account — one throttled group must not
occupy every worker slot and stall the other groups' posts.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from . import clips, crypto, media, platforms as plat, providers, service, state
from .config import MEDIA_REFRESH_MARGIN_SECONDS, settings
from .errors import (
    E_MEDIA_PENDING, E_MEDIA_TOO_LARGE, E_MEDIA_UNFETCHABLE, E_NETWORK,
    E_RATE_LIMITED, E_REMOTE_SCHEDULE, E_UNKNOWN, E_VALIDATION, ProviderError,
)
from .models import PublishAttempt, PublishDestination, PublishMedia
from .providers.base import PublishPayload

# One semaphore per credential, created on demand. Keyed by the CREDENTIAL row,
# not the group: the thing being protected is a provider account's rate limit, and
# a group can now hold several accounts (see PublishCredential.credential_slot).
# Keying on the group would serialize two independent Zernio accounts against each
# other for no reason — a 3-platform fan-out that could go out in one pass would
# take three.
_credential_locks = {}
_global_semaphore = None

# Media registration is the one step that transfers the clip's whole body: the
# provider pulls it from OUR origin. It gets its own bound, tighter than submit
# concurrency, plus a wall-clock gap between registrations — see
# config.DEFAULT_MEDIA_REGISTRATION_GAP_SECONDS. Semaphores bound concurrency;
# the gap bounds *offered load* on an origin (a home uplink or free tunnel)
# that serves one large file at a time, poorly.
_media_lock = asyncio.Lock()
_last_media_registration = 0.0


async def _media_pacer():
    """Serialize media registrations and keep a minimum gap between them.

    Held across the provider call so the gap measures transfer starts, not call
    starts. A slow origin therefore naturally lengthens the interval; the config
    value is only the floor.
    """
    global _last_media_registration
    await _media_lock.acquire()
    try:
        wait = _last_media_registration + settings.media_registration_gap_seconds \
            - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_media_registration = time.monotonic()
    except Exception:
        _media_lock.release()
        raise


def _media_pacer_done():
    try:
        _media_lock.release()
    except RuntimeError:  # pragma: no cover - defensive
        pass


def _now():
    return datetime.now(timezone.utc)


# A slot nearer than this is not worth handing over: the local dispatcher will
# reach it before a promotion pass could.
REMOTE_SCHEDULE_LEAD = timedelta(minutes=5)

# How long a publisher-only instance waits before re-checking whether the
# instance that owns the clip files has staged this one. Minutes, not seconds:
# staging is a background upload of tens of megabytes, and the slot is usually
# hours away. See _staged_info.
STAGING_WAIT_SECONDS = 300


def remote_schedule_allowed(provider) -> bool:
    """May this provider be trusted to hold the clock for a scheduled post?

    Deploy setting, then declared capability, then live health — in one place
    because two callers must never disagree. ``promote_remote_schedules`` clears
    an attempt's hold so it submits early, and the payload build decides whether
    that submit carries ``scheduledFor``. If those two answers differ, a post is
    released hours early and published immediately with no schedule at all.
    """
    if settings.remote_schedule_mode == "off":
        return False
    if not bool(getattr(provider.capabilities, "supports_remote_schedule",
                        False)):
        return False
    health = getattr(provider, "remote_schedule_ok", None)
    return bool(health()) if callable(health) else True


def global_semaphore() -> asyncio.Semaphore:
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(settings.max_concurrent_uploads)
    return _global_semaphore


def credential_semaphore(credential_id) -> asyncio.Semaphore:
    """Bound concurrent submits per provider account.

    Falls back to a shared key when there is no credential id, which only
    happens on the dry-run path — one bucket is the right answer there.
    """
    key = str(credential_id) if credential_id else "-"
    sem = _credential_locks.get(key)
    if sem is None:
        sem = asyncio.Semaphore(settings.per_credential_concurrency)
        _credential_locks[key] = sem
    return sem


async def _staged_info(session, attempt: PublishAttempt, req) -> Optional[dict]:
    """Stand in for the clip resolver in a process that holds no clip files.

    The always-on publisher runs this same dispatcher against this same database,
    but its filesystem has none of the clips: the machine that generates them is
    a different one, and may be asleep. That topology works because a clip whose
    media is already staged never needs its bytes again — ``ensure_media_ref``
    returns the cached provider ref and the provider downloads from the object
    store, not from us. Submitting at the slot is a small JSON call.

    So the only question here is whether this attempt's media ref exists and is
    still fresh. If it does, every field the dispatch path actually reads is on
    the media row. If it does not, the object store is asked directly, because a
    staged object is enough for THIS process to register the ref itself. If
    neither answers, return None so the caller PARKS the attempt. Parking rather
    than failing is the whole point: the instance that owns the files can stage it
    later, and the slot is usually still hours away.
    """
    from . import objectstore
    if not req.content_fingerprint:
        return None
    row = (await session.execute(
        select(PublishMedia).where(
            PublishMedia.publish_group_id == attempt.publish_group_id,
            PublishMedia.provider == attempt.provider,
            PublishMedia.content_fingerprint == req.content_fingerprint)
    )).scalar_one_or_none()
    if row is not None:
        margin = timedelta(seconds=MEDIA_REFRESH_MARGIN_SECONDS)
        if row.expires_at is not None and row.expires_at - margin <= _now():
            # Near expiry. Refreshing a ref means re-uploading, which needs the
            # bytes this process does not have, so park it for the owning instance
            # instead of submitting a ref that will be dead when the post goes out.
            return None
        return {
            # Never opened. ensure_media_ref short-circuits on the cached ref long
            # before anything reads this, but the caller's guard treats a missing
            # filename as "the bytes are gone", so it has to be non-empty — and
            # honest, because it does end up in log lines.
            "filename": (row.source_url or "").rsplit("/", 1)[-1] or "staged-clip",
            "fingerprint": row.content_fingerprint,
            # Real, so the platform ceiling check below still does its job.
            "size_bytes": row.size_bytes,
        }

    # No ref registered for this clip yet. Ask the store rather than parking: the
    # key is content-addressed, so a staged object is all this process needs to
    # register the ref itself — ensure_media_ref presigns the object and hands the
    # provider a URL, and neither step reads a local byte. Without this branch the
    # publisher could only send posts the app host had already fully prepared,
    # which puts the app host back on the critical path for every single slot and
    # defeats the point of moving the clock off it.
    if not media.store_available():
        return None
    key = objectstore.object_key(req.job_id, req.clip_index,
                                 req.content_fingerprint)
    try:
        head = await asyncio.to_thread(objectstore.head, key)
    except objectstore.StoreError:
        # The store is unreachable from here, which says nothing about whether the
        # object exists. Park and ask again; never fail an attempt on it.
        return None
    if not head:
        return None
    return {
        "filename": key.rsplit("/", 1)[-1],
        "fingerprint": req.content_fingerprint,
        "size_bytes": head.get("size_bytes"),
    }


async def dispatch_attempt(session, attempt: PublishAttempt,
                           claim_owner: Optional[str] = None) -> str:
    """Run one attempt to a terminal-or-parked state. Returns what happened.

    ``claim_owner`` is the worker id that claimed this attempt. Pass it whenever
    the claim was committed in an earlier transaction — which is every real
    dispatch path.
    """
    # 0. Is this claim still ours?
    #
    # Two publishing processes share this queue by design: the app host, and the
    # always-on publisher that holds the schedule clock (deploy/publisher/). The
    # claim is committed in one transaction and the work happens in another, so
    # between them another process's boot recovery can decide this row was
    # abandoned, re-queue it, claim it, and submit it. Without this check the
    # original worker then submits the same clip AGAIN — a real duplicate post to
    # a real audience, followed by an `unknown` row when record_success rejects
    # the transition, long after the damage.
    #
    # Status alone is not enough to detect that: after the other worker claims it
    # the row is `in_flight` again. The claim owner is the part that changed.
    if claim_owner is not None and (attempt.status != state.IN_FLIGHT
                                    or attempt.claimed_by != claim_owner):
        return "claim_lost"

    dest = await session.get(PublishDestination, attempt.publish_destination_id)
    if dest is None:
        await service.record_failure(session, attempt, ProviderError(
            E_VALIDATION, "destination no longer exists"))
        return "no_destination"

    # 1. Clip bytes.
    req = await _load_request(session, attempt)
    if req is None:
        await service.record_failure(session, attempt, ProviderError(
            E_VALIDATION, "publish request no longer exists"))
        return "no_request"

    info = clips.resolve(req.job_id, req.clip_index)
    if info is None and not clips.has_resolver():
        # No resolver is registered at all, which means this process holds no
        # clips by design (the publisher-only instance; app.py registers one).
        # Distinguishing that from "this clip is gone" is essential: the guard
        # below is PERMANENT, so without this branch a publisher would fail every
        # attempt in the queue seconds after boot — including posts whose media is
        # staged, paid for and ready — and blame job retention for it.
        info = await _staged_info(session, attempt, req)
        if info is None:
            await service.record_deferral(
                session, attempt, STAGING_WAIT_SECONDS,
                "waiting for this clip's media to be staged by the instance "
                "that holds the files")
            return "awaiting_staging"
    if info is None or not info.get("filename"):
        # Retention (JOB_RETENTION_SECONDS) removes clips 24h after a job ends,
        # so this is expected for a long-deferred post. Permanent: the bytes are
        # not coming back.
        await service.record_failure(session, attempt, ProviderError(
            E_VALIDATION,
            "the clip file is no longer available (job retention expired). "
            "Re-generate the clip and publish again."))
        return "clip_missing"

    # 2. Destination health.
    if not dest.enabled or dest.health in ("blocked", "disconnected"):
        attempt.status = state.BLOCKED
        attempt.completed_at = _now()
        attempt.error_code = "destination_unavailable"
        attempt.error_message = f"destination is {dest.health}"
        await service.refresh_request_status(session, attempt.publish_request_id)
        return "blocked"

    # 3. Provider-imposed spacing.
    if dest.cooldown_until and dest.cooldown_until > _now():
        wait = int((dest.cooldown_until - _now()).total_seconds())
        await service.release_claim(session, attempt, defer_seconds=max(wait, 30))
        return "cooldown"

    # 4. Quota. Only trusted while the cached window is still current: a stale
    # 'remaining: 0' from yesterday must not block today's posts.
    if (dest.quota_remaining is not None and dest.quota_remaining <= 0
            and dest.quota_reset_at and dest.quota_reset_at > _now()):
        wait = int((dest.quota_reset_at - _now()).total_seconds())
        await service.record_deferral(
            session, attempt, max(wait, 60),
            f"daily quota exhausted for {dest.platform}; waiting for reset")
        return "quota"

    # 5. Media ceilings — the tightest limit across this destination's platform.
    reason = plat.check_video(dest.platform, info.get("size_bytes"),
                              info.get("duration"))
    if reason:
        await service.record_failure(session, attempt, ProviderError(
            E_MEDIA_TOO_LARGE, reason))
        return "too_large"

    credential = await service.credential_for_destination(session, dest)
    if credential is None:
        # Parking (rather than failing) is deliberate: the operator can still fix
        # the key and every queued post goes out. But this path consumes no try,
        # so it repeats every 15 minutes indefinitely — and until 2026-08-17 it
        # wrote nothing to the row, which is how a group sat "Scheduled, next
        # 12:38" for eleven hours with nothing on the provider and no reason
        # visible anywhere. The reason goes on the attempt, and the audit line is
        # written once per attempt instead of once per tick.
        #
        # The slot is named in every message below. On a multi-account group
        # "this group has no API key" would be actively misleading: the group has
        # a key, just not for the account THIS destination publishes through, and
        # the fix is a different field in a different row.
        slot = (dest.credential_slot or "").strip()
        whose = f"account “{slot}”" if slot else "this group"
        rejected = await service.credential_for_destination(
            session, dest, include_invalid=True)
        if rejected is not None:
            why = (f"the provider rejected the API key for {whose}, so nothing "
                   f"can be sent. Re-check or replace it under Publishing → "
                   f"this group → credential; the post is held, not lost.")
            if rejected.invalid_reason:
                why = f"{why} Provider said: {rejected.invalid_reason[:300]}"
        else:
            why = (f"there is no API key stored for {whose}, so nothing can be "
                   f"sent. Add one under Publishing → this group → credential; "
                   f"the post is held, not lost.")
        first = attempt.error_code != "no_credential"
        await service.release_claim(session, attempt, defer_seconds=900,
                                    reason=why, code="no_credential")
        if first:
            await service.log_event(
                session, "dispatch.no_credential",
                message=f"no usable API key for {whose}",
                request_id=attempt.publish_request_id, attempt_id=attempt.id,
                destination_id=dest.id,
                group_id=dest.publish_group_id,
                data={"slot": slot or None})
        return "no_credential"

    try:
        api_key = crypto.decrypt({
            "key_version": credential.key_version,
            "nonce_b64": credential.nonce_b64,
            "ciphertext_b64": credential.ciphertext_b64,
            "aad": credential.aad,
        })
    except Exception as e:
        first = attempt.error_code != "credential_unreadable"
        await service.release_claim(
            session, attempt, defer_seconds=1800,
            reason=("this group's stored API key cannot be decrypted — "
                    "PUBLISHING_MASTER_KEY has probably changed. Re-enter the "
                    "key to re-seal it under the current master key."),
            code="credential_unreadable")
        if first:
            await service.log_event(
                session, "dispatch.credential_unreadable",
                message=crypto.scrub(str(e))[:500],
                request_id=attempt.publish_request_id, attempt_id=attempt.id,
                group_id=dest.publish_group_id)
        return "credential_unreadable"

    provider = providers.get(dest.provider)
    by_ref = bool(getattr(provider.capabilities, "supports_media_refs", False))

    # 6. Media. Two shapes, one for each half of the capability matrix:
    #    ref-based — upload once per (group, content) and reuse the ref across
    #    platforms; ref-less — hand over a presigned URL and let the provider
    #    fetch at submit time. Both raise the same errors, so the handling below
    #    is shared.
    media_url = None
    try:
        media_row = await ensure_media_ref(
            session, provider, api_key.reveal(), dest, req, info)
        if not by_ref:
            media_url = await ensure_media_url(req, info)
    except ProviderError as err:
        if err.code == E_MEDIA_PENDING:
            # The clip is still being copied to the object store the provider
            # fetches from. Park — do NOT consume a try: on a slow uplink a
            # 20 MB clip can take twenty minutes, and burning the retry budget
            # waiting for our own transfer would kill posts that are fine.
            first = attempt.error_code != "media_transfer_pending"
            await service.release_claim(
                session, attempt, defer_seconds=120,
                reason=("the clip is still being copied to the media store so "
                        "the provider can fetch it quickly. The post is held, "
                        "not lost, and goes out as soon as the copy finishes."),
                code="media_transfer_pending")
            if first:
                await service.log_event(
                    session, "dispatch.media_pending",
                    message=err.message[:500],
                    request_id=attempt.publish_request_id,
                    attempt_id=attempt.id, group_id=dest.publish_group_id)
            return "media_pending"
        if err.code == E_MEDIA_UNFETCHABLE and by_ref:
            # At the REGISTRATION step, "provider could not download our URL" is
            # almost always origin congestion (the tunnel saturating, observed
            # 2026-08-15), not a permanent property of the clip. The post was
            # never created, so retrying with backoff is safe; classifying it
            # permanent here is what killed a whole day's posts in one incident.
            #
            # Scoped to the ref-based path on purpose. A ref-less provider has
            # not been asked for anything yet, so the same code there means
            # something entirely different — we could not produce a URL at all,
            # which is a store/origin misconfiguration and not congestion. Calling
            # that "will retry with backoff" would retry a config error five times
            # and describe it wrongly each time.
            err = ProviderError(
                E_NETWORK,
                f"provider could not fetch the media URL yet — likely origin "
                f"congestion; will retry with backoff ({err.message})",
                status_code=err.status_code, response=err.response)
        await service.record_failure(session, attempt, err,
                                     credential_id=credential.id)
        return f"media_{err.code}"

    attempt.publish_media_id = media_row.id if media_row else None
    title, caption = clips.build_caption(info, req.payload, dest.platform)
    options = dict((dest.settings or {}).get("options") or {})
    options.update(((req.payload or {}).get("per_platform") or {})
                   .get(plat.normalize(dest.platform), {}).get("options") or {})

    # Remote scheduling window: a submit carrying scheduled_for hands the CLOCK
    # to the provider (submit now, publish at the appointed time). Gated on the
    # same answer the promote pass used, so a post is never released early and
    # then submitted without the field — that publishes it immediately.
    remote_time = None
    if (req.scheduled_for is not None
            and req.scheduled_for > _now() + REMOTE_SCHEDULE_LEAD
            and remote_schedule_allowed(provider)):
        remote_time = req.scheduled_for

    payload = PublishPayload(
        platform=dest.platform,
        provider_account_ref=dest.provider_account_ref,
        caption=caption,
        title=title,
        media_ref=media_row.provider_media_ref if media_row else None,
        media_url=media_url,
        scheduled_for=remote_time,
        options=options,
    )

    # 7. Submit.
    async with global_semaphore():
        async with credential_semaphore(credential.id):
            try:
                result = await provider.submit(api_key.reveal(), payload)
            except ProviderError as err:
                if err.code == E_REMOTE_SCHEDULE:
                    # The provider refused the schedule FIELD, not the post: a
                    # 4xx created nothing. Stop offering the field for this
                    # process and let the local clock take the slot.
                    disable = getattr(provider, "disable_remote_schedule", None)
                    if callable(disable):
                        disable(err.message)
                    await service.release_claim(
                        session, attempt,
                        defer_seconds=max(
                            30, int((req.scheduled_for - _now())
                                    .total_seconds())) if req.scheduled_for
                        else 300)
                    await service.log_event(
                        session, "attempt.remote_schedule_fallback",
                        message="provider rejected remote scheduling; holding "
                                "the clock locally",
                        request_id=attempt.publish_request_id,
                        attempt_id=attempt.id,
                        destination_id=attempt.publish_destination_id,
                        group_id=attempt.publish_group_id)
                    await service.refresh_request_status(
                        session, attempt.publish_request_id)
                    return "remote_fallback"
                await service.record_failure(session, attempt, err,
                                             credential_id=credential.id)
                if err.code == E_RATE_LIMITED and err.defer_seconds:
                    dest.cooldown_until = _now() + timedelta(
                        seconds=err.defer_seconds)
                return f"failed_{err.code}"
            except Exception as e:
                # An unclassified exception after the request left the process is
                # ambiguous by definition: the post may exist. Never auto-retried.
                await service.record_failure(session, attempt, ProviderError(
                    E_UNKNOWN, f"unclassified submit error: {e}"),
                    credential_id=credential.id)
                return "failed_unknown"

    credential.last_used_at = _now()
    if result.status == "deferred":
        await service.record_deferral(
            session, attempt, result.defer_seconds or 3600,
            "provider queued this post for the next available window",
            quota=result.quota)
        return "deferred"

    await service.record_success(session, attempt, result,
                                 credential_id=credential.id)
    if getattr(result, "schedule_ignored", False):
        # The provider took a future timestamp and published anyway. The post is
        # live and stays live — never resubmitted, that would duplicate it — but
        # this is the loudest thing the log can say, because every remaining slot
        # in the plan is about to go out at once unless the clock comes home. The
        # adapter has already disabled hand-over for this process; the event is
        # what tells the operator why their spacing evaporated.
        asked = (req.scheduled_for.isoformat() if req.scheduled_for
                 else "a future slot")
        await service.log_event(
            session, "attempt.remote_schedule_ignored",
            message=f"provider published immediately despite {asked}; "
                    f"holding the clock locally from now on",
            request_id=attempt.publish_request_id, attempt_id=attempt.id,
            destination_id=attempt.publish_destination_id,
            group_id=attempt.publish_group_id,
            data={"asked_for": req.scheduled_for.isoformat()
                  if req.scheduled_for else None,
                  "provider_response": crypto.scrub(str(result.raw))[:2000]})
    if dest.health == "unverified":
        # First successful publish IS the verification — there is no listing or
        # dry-run endpoint to do it beforehand.
        dest.health = "ok"
        dest.health_detail = "confirmed by a successful publish"
        dest.verified_at = _now()
    return result.status


async def ensure_media_ref(session, provider, api_key: str,
                           dest: PublishDestination, req, info: dict
                           ) -> Optional[PublishMedia]:
    """Get a reusable provider media ref, uploading only when necessary.

    Cache key is ``(group, provider, content_fingerprint)``. Scoped to the group
    because cross-credential reuse of a ref is NOT documented, and a ref that
    turns out to be account-scoped would fail on another group's key. If reuse
    across keys is later confirmed, widening this key is a one-line change with
    no schema impact.

    That scope has one edge it does not cover: a provider that is BOTH ref-based
    and multi-credential (several provider accounts in one group) would share one
    ref across two accounts, and an account-scoped ref would fail on the second.
    No provider is both today — Status 200 is single-credential, Zernio is
    ref-less — and ``Capabilities`` states each half, so the combination is
    checkable rather than latent. It would need ``credential_slot`` on the cache
    key, which is a schema change; hence the note instead of the code.

    A near-expiry ref is re-uploaded rather than used: refs roll off after 7 days
    and a scheduled post must never submit a dead one.
    """
    if not getattr(provider.capabilities, "supports_media_refs", False):
        return None

    fingerprint = info.get("fingerprint") or req.content_fingerprint
    if not fingerprint:
        raise ProviderError(E_VALIDATION, "clip has no content fingerprint")

    row = (await session.execute(
        select(PublishMedia).where(
            PublishMedia.publish_group_id == dest.publish_group_id,
            PublishMedia.provider == dest.provider,
            PublishMedia.content_fingerprint == fingerprint)
    )).scalar_one_or_none()

    margin = timedelta(seconds=MEDIA_REFRESH_MARGIN_SECONDS)
    if row is not None:
        fresh = row.expires_at is None or row.expires_at - margin > _now()
        if fresh:
            row.last_used_at = _now()
            return row

    public_url, strategy = await media.public_url_for_clip(
        req.job_id, req.clip_index, info["filename"],
        fingerprint=fingerprint, user_id=info.get("user_id"),
        # Long-lived on purpose: the provider stores this URL and downloads from
        # it when the post goes out, not now. See the setting's docstring.
        ttl_seconds=settings.provider_media_url_ttl_seconds)
    if media.is_pending(strategy):
        raise ProviderError(E_MEDIA_PENDING, strategy)
    if not public_url:
        raise ProviderError(
            E_MEDIA_UNFETCHABLE,
            f"cannot expose the clip to the provider: {strategy}")

    ref = None
    await _media_pacer()
    try:
        ref = await provider.upload_media(
            api_key, media_url=public_url, mime_type="video/mp4")
    finally:
        _media_pacer_done()

    if row is None:
        row = PublishMedia(
            publish_group_id=dest.publish_group_id,
            provider=dest.provider,
            content_fingerprint=fingerprint,
            job_id=req.job_id,
            clip_index=req.clip_index,
            provider_media_ref=ref.ref,
            source_url=None,  # a signed URL is a secret; never persisted
            size_bytes=ref.size_bytes or info.get("size_bytes"),
            mime_type=ref.mime_type,
            expires_at=ref.expires_at,
            last_used_at=_now(),
        )
        session.add(row)
    else:
        row.provider_media_ref = ref.ref
        row.expires_at = ref.expires_at
        row.size_bytes = ref.size_bytes or info.get("size_bytes")
        row.mime_type = ref.mime_type
        row.last_used_at = _now()
    await session.flush()
    return row


async def ensure_media_url(req, info: dict) -> str:
    """Presigned URL for a provider that takes media by URL and keeps no ref.

    The counterpart to ``ensure_media_ref`` for the other half of the capability
    matrix. There is nothing to cache and nothing to register: the URL goes
    straight into the submit body and the provider fetches from it, so this is a
    signing call against the staged object, not a transfer.

    Which makes the TTL the load-bearing part. It is the long provider TTL (7
    days), not the short one used for browser playback, because a scheduled post
    hands this URL over now and the provider downloads at the slot — hours or
    days later. A one-hour URL here is a post that looks perfectly healthy until
    the moment it goes out and then fails with "could not download".
    """
    fingerprint = info.get("fingerprint") or req.content_fingerprint
    if not fingerprint:
        raise ProviderError(E_VALIDATION, "clip has no content fingerprint")
    public_url, strategy = await media.public_url_for_clip(
        req.job_id, req.clip_index, info["filename"],
        fingerprint=fingerprint, user_id=info.get("user_id"),
        ttl_seconds=settings.provider_media_url_ttl_seconds)
    if media.is_pending(strategy):
        # Still copying to the store. The caller parks without consuming a try.
        raise ProviderError(E_MEDIA_PENDING, strategy)
    if not public_url:
        raise ProviderError(
            E_MEDIA_UNFETCHABLE,
            f"cannot expose the clip to the provider: {strategy}")
    return public_url


# Pre-registration backoff: a clip whose registration failed PERMANENTLY (too
# large, unfetchable for a structural reason) must not be retried every tick.
# In-memory because this is an optimization, not state: dispatch re-checks and
# re-tries whatever it must when the attempt's own clock comes due.
_preregister_skipped: dict = {}
_PREREGISTER_SKIP_TTL = 3600.0


async def preregister_next_media(session) -> int:
    """Register media for ONE queued post that does not have a ref yet.

    Runs on the reconciler tick (default 60s), so a finished job's clips are
    transferred to the provider one per tick at plan time — spread out, ahead of
    their slots — instead of all at once at dispatch time. At dispatch the ref is
    already cached (ensure_media_ref is a no-op then) and submitting is a tiny
    JSON call: nothing large moves through the origin when posts go out.

    Returns 0 or 1. One per pass is deliberate: combined with the pacer it is
    what keeps offered load at one clip per gap.
    """
    from .models import PublishRequest
    from sqlalchemy import select as _select

    now = _now()
    rows = (await session.execute(
        _select(PublishAttempt, PublishRequest)
        .join(PublishRequest,
              PublishRequest.id == PublishAttempt.publish_request_id)
        .where(PublishAttempt.status.in_([state.PENDING, state.DEFERRED]))
        .where(PublishRequest.content_fingerprint.is_not(None))
        .order_by(PublishRequest.scheduled_for.asc().nullsfirst(),
                  PublishAttempt.created_at.asc())
        .limit(60)
    )).all()

    seen = set()
    for attempt, req in rows:
        key = (str(attempt.publish_group_id), attempt.provider,
               req.content_fingerprint)
        if key in seen:
            continue
        seen.add(key)
        skip = _preregister_skipped.get(key)
        if skip and skip["until"] > now:
            continue

        try:
            provider = providers.get(attempt.provider)
        except KeyError:
            continue
        if not getattr(provider.capabilities, "supports_media_refs", False):
            # Nothing to pre-register: this provider takes media by URL at submit
            # time and keeps no ref. Skipping is not merely an optimisation —
            # without it ``ensure_media_ref`` returns None, no PublishMedia row is
            # ever written, so the ``have`` check below can never become true and
            # this pass would report a fresh "media.preregistered" every single
            # tick, forever, for a registration that did not happen.
            continue

        info = clips.resolve(req.job_id, req.clip_index)
        if info is None or not info.get("filename"):
            # Skip, and let dispatch decide what it means: a permanent failure
            # where clips exist and this one is gone, a park where the process
            # holds no clips at all. Pre-registration is an optimisation and has
            # no business making that call.
            continue

        have = (await session.execute(
            _select(PublishMedia.id).where(
                PublishMedia.publish_group_id == attempt.publish_group_id,
                PublishMedia.provider == attempt.provider,
                PublishMedia.content_fingerprint == req.content_fingerprint)
        )).scalar_one_or_none()
        if have is not None:
            continue

        dest = (await session.execute(
            _select(PublishDestination).where(
                PublishDestination.publish_group_id
                == attempt.publish_group_id,
                PublishDestination.enabled.is_(True))
            .limit(1)
        )).scalar_one_or_none()
        if dest is None:
            continue

        # Through the destination, not the group: the arbitrary destination
        # picked above may name a credential slot, and a group-wide lookup could
        # hand back a different provider account's key. Registering a ref under
        # the wrong account is how a ref that is account-scoped fails at submit
        # time, days later, on a post that pre-registered cleanly.
        credential = await service.credential_for_destination(session, dest)
        if credential is None:
            continue
        try:
            api_key = crypto.decrypt({
                "key_version": credential.key_version,
                "nonce_b64": credential.nonce_b64,
                "ciphertext_b64": credential.ciphertext_b64,
                "aad": credential.aad,
            })
        except Exception as e:
            print(f"⚠️  Publishing: pre-registration could not read the key "
                  f"for group {dest.publish_group_id}: {crypto.scrub(str(e))}")
            return 0

        try:
            await ensure_media_ref(session, provider, api_key.reveal(),
                                   dest, req, info)
            _preregister_skipped.pop(key, None)
            await service.log_event(
                session, "media.preregistered",
                message=f"{req.job_id}[{req.clip_index}] registered ahead of "
                        f"its slot",
                group_id=dest.publish_group_id)
            return 1
        except ProviderError as err:
            if err.code == E_MEDIA_PENDING:
                # Still being copied to the object store. Nothing to register
                # yet, but a LATER clip may already be staged — a 20-minute
                # upload at the head of the queue must not stall the rest.
                continue
            if not err.retryable:
                # Permanent at the registration step too — do not hammer it
                # every tick; dispatch will surface it on the attempt itself.
                _preregister_skipped[key] = {
                    "code": err.code, "until": now + timedelta(
                        seconds=_PREREGISTER_SKIP_TTL)}
            # Transient errors: leave it; the next tick retries naturally.
            return 0
    return 0


async def promote_remote_schedules(session) -> int:
    """Release scheduled attempts for EARLY submit when the provider can hold
    the clock.

    Attempts are created with ``deferred_until = scheduled_for`` — the local
    clock, which is where it stays for every provider that cannot be *proven*
    to honour a timestamp. ``remote_schedule_allowed`` is the single authority
    (the payload build asks it the same question); when it says yes this pass
    clears the hold so the dispatcher claims the attempt now and submits it
    carrying ``scheduledFor``: media moves immediately, the provider publishes
    at the slot, and this machine can be off at the appointed time.

    A no-op for a provider that cannot hold a clock; for Status 200 it is the
    normal path, and the timestamp format is what makes it work (``_iso_z``).

    Idempotent and narrow on purpose: only rows still parked exactly on their
    slot time are touched. A row deferred by cooldown, quota or a media retry
    keeps its own clock and self-heals on its next dispatch.
    """
    # Cheap short-circuit before touching the database. The per-provider answer
    # below still comes from remote_schedule_allowed, never from this check.
    if settings.remote_schedule_mode == "off":
        return 0

    from .models import PublishRequest
    horizon = _now() + REMOTE_SCHEDULE_LEAD
    rows = (await session.execute(
        select(PublishAttempt, PublishRequest)
        .join(PublishRequest,
              PublishRequest.id == PublishAttempt.publish_request_id)
        .where(PublishAttempt.status.in_([state.PENDING, state.DEFERRED]))
        .where(PublishRequest.scheduled_for > horizon)
        .where(PublishAttempt.deferred_until == PublishRequest.scheduled_for)
        .limit(200)
    )).all()

    # Provider-level gating, resolved once per provider name.
    allowed: dict = {}
    for attempt, _req in rows:
        if attempt.provider in allowed:
            continue
        try:
            provider = providers.get(attempt.provider)
        except KeyError:
            allowed[attempt.provider] = False
            continue
        allowed[attempt.provider] = remote_schedule_allowed(provider)

    promoted = 0
    for attempt, req in rows:
        if not allowed.get(attempt.provider):
            continue
        attempt.deferred_until = None
        promoted += 1
        await service.log_event(
            session, "attempt.promoted_remote",
            message=f"handed to the provider for {req.scheduled_for.isoformat()}",
            request_id=attempt.publish_request_id, attempt_id=attempt.id,
            destination_id=attempt.publish_destination_id,
            group_id=attempt.publish_group_id,
            data={"scheduled_for": req.scheduled_for})
    if promoted:
        await session.flush()
    return promoted


async def poll_submitted_attempts(session) -> int:
    """Ask a pollable provider whether each submitted post has gone live.

    The counterpart to ``sweep_stale_submitted`` and the reason it now runs
    first. For years the ONLY completion signal was a webhook, because the only
    provider had no status endpoint; a post whose callback was lost had exactly
    one ending — the sweeper aged it into ``unknown`` after
    ``submit_timeout_seconds`` and a human had to go and look at the account.

    A provider that declares ``supports_status_lookup`` can simply be asked, so
    this pass resolves such a post the moment it is due instead of condemning it.
    It changes nothing for Status 200 (``supports_status_lookup=False``): its
    attempts never match the provider filter, so the sweeper stays their only
    backstop.

    Safety is inherited, not re-derived. ``fetch_status`` returns a
    ``SubmitResult`` whose ``status`` is exactly what a submit returns, so the
    outcomes below route through the SAME service calls a submit or a webhook
    would — ``record_success`` for a confirmed post, ``record_failure`` for a
    definite failure (which never fires on an ambiguous one, because the adapter
    raises ``E_UNKNOWN`` for that and it lands in the ambiguous branch and stays
    ``unknown``). Nothing here invents a transition the state machine forbids.

    Rate-limited three ways so a flapping provider cannot become a request flood:
    a post is left alone until ``status_poll_min_age_seconds`` after submit, then
    re-asked no more than once per ``status_poll_interval_seconds`` (both
    enforced by the persisted ``last_polled_at``, which survives a restart), and
    at most ``status_poll_batch`` posts are polled per pass because this is
    serial provider I/O inside one transaction.
    """
    if not settings.status_poll_enabled:
        return 0

    now = _now()
    min_age = timedelta(seconds=settings.status_poll_min_age_seconds)
    interval = timedelta(seconds=settings.status_poll_interval_seconds)

    # A submitted attempt with a provider ref, old enough to be worth asking
    # about and not asked too recently. A future `deferred_until` is a post the
    # provider deliberately parked for a later window (a daily-cap 202) — silence
    # before then is expected, so it is skipped exactly as the sweeper skips it.
    rows = (await session.execute(
        select(PublishAttempt)
        .where(PublishAttempt.status == state.SUBMITTED,
               PublishAttempt.provider_post_ref.is_not(None),
               PublishAttempt.submitted_at.is_not(None),
               PublishAttempt.submitted_at < now - min_age,
               (PublishAttempt.last_polled_at.is_(None))
               | (PublishAttempt.last_polled_at < now - interval),
               (PublishAttempt.deferred_until.is_(None))
               | (PublishAttempt.deferred_until < now))
        .order_by(PublishAttempt.last_polled_at.asc().nulls_first(),
                  PublishAttempt.submitted_at.asc())
        .limit(settings.status_poll_batch)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    polled = 0
    for attempt in rows:
        # The SQL above already applied these three gates; this re-check is the
        # authority. `state.poll_is_due` is the pure, CI-testable statement of the
        # rule, and re-asking it here means a drift between the query and the
        # policy can only ever skip a poll, never add one.
        if not state.poll_is_due(
                now,
                submitted_at=attempt.submitted_at,
                last_polled_at=attempt.last_polled_at,
                deferred_until=attempt.deferred_until,
                min_age_seconds=settings.status_poll_min_age_seconds,
                interval_seconds=settings.status_poll_interval_seconds):
            continue
        try:
            provider = providers.get(attempt.provider)
        except KeyError:
            continue
        if not getattr(provider.capabilities, "supports_status_lookup", False):
            # Not pollable. Left for the stale sweeper. Not stamped, so if the
            # provider gains the capability later this post is not stuck looking
            # freshly polled.
            continue

        lookup = getattr(provider, "fetch_status", None)
        if lookup is None:
            continue

        # The credential is resolved through the destination, not the group: on a
        # multi-account group the ref belongs to ONE provider account, and asking
        # a different account about it earns a 404 that reads identically to a
        # deleted post — a false negative that would age a live post into unknown.
        dest = await session.get(
            PublishDestination, attempt.publish_destination_id)
        if dest is None:
            continue
        credential = await service.credential_for_destination(session, dest)
        if credential is None:
            # No usable key for this account right now. Dispatch's no_credential
            # path already surfaces that on the row; polling has nothing to add
            # and must not stamp, or a key fixed later would wait a full interval.
            continue
        try:
            api_key = crypto.decrypt({
                "key_version": credential.key_version,
                "nonce_b64": credential.nonce_b64,
                "ciphertext_b64": credential.ciphertext_b64,
                "aad": credential.aad,
            })
        except Exception:
            # Decrypt failures are dispatch's to report (credential_unreadable).
            continue

        # Stamp BEFORE the call, not after: an adapter that raises here (a 5xx on
        # the status endpoint) must still count as "asked just now", or a provider
        # erroring every lookup would be re-polled on every tick with no rate
        # limit at all.
        attempt.last_polled_at = now
        polled += 1
        try:
            result = await lookup(api_key.reveal(), attempt.provider_post_ref)
        except ProviderError as err:
            if err.is_ambiguous:
                # The lookup itself was inconclusive. Nothing changes; the post
                # stays submitted and the sweeper remains its final backstop.
                continue
            # A classified failure reported by a status lookup describes the POST,
            # not the request: the provider is telling us the publish did not
            # happen. Route it through the same failure path a submit would, so a
            # retryable code retries with backoff and a destination-fatal one
            # (E_ACCOUNT_AUTH) marks the account for re-linking — the SUBMITTED ->
            # BLOCKED move the state machine now permits.
            await service.record_failure(session, attempt, err,
                                         credential_id=credential.id)
            continue
        except Exception:
            # An unclassified error asking about a post tells us nothing about the
            # post. Leave it submitted; it was stamped, so not re-asked before the
            # next interval.
            continue

        if result is None:
            # "No information" — unreachable, or a 404 that a deleted post and a
            # never-valid ref share. Not evidence of failure; left for the
            # sweeper's unknown.
            continue

        if result.status == "succeeded":
            # Applied like a webhook confirmation, NOT via record_success: that
            # helper is the submit path and stamps `submitted_at = now`, which
            # would rewrite when the post was handed over to when we happened to
            # ask about it — the one timestamp an operator uses to reconstruct a
            # lost callback. It also logs its own `attempt.succeeded`, so calling
            # it here would double the audit line.
            state.assert_transition(attempt.status, state.SUCCEEDED)
            attempt.status = state.SUCCEEDED
            attempt.completed_at = now
            attempt.provider_native_post_ref = (
                result.provider_native_post_ref
                or attempt.provider_native_post_ref)
            attempt.permalink = result.permalink or attempt.permalink
            if dest.health == "unverified":
                # Same reasoning as the submit path: with no listing or dry-run
                # endpoint, a publish that demonstrably landed IS the
                # verification.
                dest.health = "ok"
                dest.health_detail = "confirmed by a successful publish"
                dest.verified_at = now
            await service.log_event(
                session, "attempt.succeeded",
                message="confirmed by status poll",
                request_id=attempt.publish_request_id, attempt_id=attempt.id,
                destination_id=attempt.publish_destination_id,
                group_id=attempt.publish_group_id)
            await service.refresh_request_status(
                session, attempt.publish_request_id)
        # Any other status ("submitted", a draft) is "still pending": the post
        # exists but has not resolved. Nothing to write beyond the stamp already
        # applied; the next due pass asks again.

    return polled


async def _load_request(session, attempt: PublishAttempt):
    from .models import PublishRequest
    return await session.get(PublishRequest, attempt.publish_request_id)
