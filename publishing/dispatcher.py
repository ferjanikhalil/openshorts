"""Taking one claimed attempt and actually publishing it.

The order of checks here is the whole design. Each one is cheaper than the next,
and each avoids spending something scarce on work that cannot succeed:

  1. clip resolvable?      -> free; a deleted clip can never publish
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
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from . import clips, crypto, media, platforms as plat, providers, service, state
from .config import MEDIA_REFRESH_MARGIN_SECONDS, settings
from .errors import (
    E_MEDIA_TOO_LARGE, E_MEDIA_UNFETCHABLE, E_RATE_LIMITED, E_UNKNOWN,
    E_VALIDATION, ProviderError,
)
from .models import PublishAttempt, PublishDestination, PublishMedia
from .providers.base import PublishPayload

# One semaphore per credential, created on demand. Keyed by group because the
# credential is per group.
_group_locks = {}
_global_semaphore = None


def _now():
    return datetime.now(timezone.utc)


def global_semaphore() -> asyncio.Semaphore:
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(settings.max_concurrent_uploads)
    return _global_semaphore


def group_semaphore(group_id) -> asyncio.Semaphore:
    key = str(group_id)
    sem = _group_locks.get(key)
    if sem is None:
        sem = asyncio.Semaphore(settings.per_credential_concurrency)
        _group_locks[key] = sem
    return sem


async def dispatch_attempt(session, attempt: PublishAttempt) -> str:
    """Run one attempt to a terminal-or-parked state. Returns what happened."""
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

    credential = await service.active_credential(session, dest.publish_group_id)
    if credential is None:
        await service.release_claim(session, attempt, defer_seconds=900)
        await service.log_event(
            session, "dispatch.no_credential",
            message="no usable API key for this group",
            request_id=attempt.publish_request_id, attempt_id=attempt.id,
            group_id=dest.publish_group_id)
        return "no_credential"

    try:
        api_key = crypto.decrypt({
            "key_version": credential.key_version,
            "nonce_b64": credential.nonce_b64,
            "ciphertext_b64": credential.ciphertext_b64,
            "aad": credential.aad,
        })
    except Exception as e:
        await service.release_claim(session, attempt, defer_seconds=1800)
        await service.log_event(
            session, "dispatch.credential_unreadable",
            message=crypto.scrub(str(e))[:500],
            request_id=attempt.publish_request_id, attempt_id=attempt.id,
            group_id=dest.publish_group_id)
        return "credential_unreadable"

    provider = providers.get(dest.provider)

    # 6. Media ref — uploaded once per (group, content), reused across platforms.
    try:
        media_row = await ensure_media_ref(
            session, provider, api_key.reveal(), dest, req, info)
    except ProviderError as err:
        await service.record_failure(session, attempt, err,
                                     credential_id=credential.id)
        return f"media_{err.code}"

    attempt.publish_media_id = media_row.id if media_row else None
    title, caption = clips.build_caption(info, req.payload, dest.platform)
    options = dict((dest.settings or {}).get("options") or {})
    options.update(((req.payload or {}).get("per_platform") or {})
                   .get(plat.normalize(dest.platform), {}).get("options") or {})

    payload = PublishPayload(
        platform=dest.platform,
        provider_account_ref=dest.provider_account_ref,
        caption=caption,
        title=title,
        media_ref=media_row.provider_media_ref if media_row else None,
        media_url=None if media_row else info.get("public_url"),
        options=options,
    )

    # 7. Submit.
    async with global_semaphore():
        async with group_semaphore(dest.publish_group_id):
            try:
                result = await provider.submit(api_key.reveal(), payload)
            except ProviderError as err:
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

    public_url, strategy = media.public_url_for_clip(
        req.job_id, req.clip_index, info["filename"],
        user_id=info.get("user_id"))
    if not public_url:
        raise ProviderError(
            E_MEDIA_UNFETCHABLE,
            f"cannot expose the clip to the provider: {strategy}")

    ref = await provider.upload_media(
        api_key, media_url=public_url, mime_type="video/mp4")

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


async def _load_request(session, attempt: PublishAttempt):
    from .models import PublishRequest
    return await session.get(PublishRequest, attempt.publish_request_id)
