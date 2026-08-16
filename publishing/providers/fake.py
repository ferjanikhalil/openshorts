"""In-repo fake provider — the reason this system is testable before credentials.

With ``PUBLISHING_DRY_RUN=1`` every provider lookup resolves here, so the whole
pipeline (enqueue, media upload, dispatch, retries, quota deferral, webhooks,
derived status, admin UI) can be exercised end to end without a Status 200 key
and without publishing anything anywhere.

It is not a stub that always succeeds — a fake that only models the happy path
would let exactly the bugs that matter through. Behaviour is driven by markers in
the caption or the account ref, so a test (or a human clicking around) can force
each failure mode deterministically:

  ``fail-auth``       -> 401-equivalent, credential marked invalid
  ``fail-blocked``    -> 403-equivalent, destination marked blocked
  ``fail-transient``  -> retryable network error
  ``fail-oversize``   -> permanent media-too-large
  ``fail-unknown``    -> ambiguous timeout (must never auto-retry)
  ``fail-ambiguous``  -> gateway 5xx with no provider body (also ambiguous)
  ``quota``           -> 202-equivalent: the post is PARKED, so submitted with a
                         defer window — never re-sent
  ``refuse-capacity`` -> provider created nothing and said "later" (true deferral)
  ``slow``            -> submitted, awaiting a webhook
  anything else       -> published immediately
"""
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import platforms as plat
from ..errors import (
    E_AUTH, E_MEDIA_TOO_LARGE, E_NETWORK, E_NOT_CONNECTED, E_UNKNOWN,
    ProviderError,
)
from .base import (
    Capabilities, MediaRef, PublishPayload, SubmitResult, WebhookEvent,
)

MEDIA_TTL_SECONDS = 7 * 24 * 3600

CAPABILITIES = Capabilities(
    name="fake",
    platforms=(plat.YOUTUBE, plat.INSTAGRAM, plat.TIKTOK),
    supports_media_refs=True,
    media_by_url=True,
    media_ref_ttl_seconds=MEDIA_TTL_SECONDS,
    # Mirrors Status 200 exactly, so dry-run exercises the SAME orchestration
    # branches the real provider will take. A fake with richer capabilities would
    # test code paths that never run in production.
    supports_status_lookup=False,
    supports_remote_schedule=False,
    supports_cancel_scheduled=False,
    supports_account_listing=False,
    supports_webhooks=True,
    one_platform_per_request=True,
)

# Everything the fake did, for assertions and for the dry-run admin view.
submissions = []
uploads = []


def reset():
    submissions.clear()
    uploads.clear()


def _marker(payload: PublishPayload) -> str:
    blob = f"{payload.caption or ''} {payload.provider_account_ref or ''}".lower()
    for m in ("fail-auth", "fail-blocked", "fail-transient", "fail-oversize",
              "fail-unknown", "fail-ambiguous", "refuse-capacity", "quota",
              "slow"):
        if m in blob:
            return m
    return ""


class FakeProvider:
    capabilities = CAPABILITIES

    async def upload_media(self, api_key: str, *, media_url: str,
                           mime_type: Optional[str] = None) -> MediaRef:
        if not api_key:
            raise ProviderError(E_AUTH, "no api key supplied")
        if "fail-oversize" in (media_url or ""):
            raise ProviderError(E_MEDIA_TOO_LARGE, "fake: media too large")
        if "fail-transient" in (media_url or ""):
            raise ProviderError(E_NETWORK, "fake: transient upload failure")
        ref = "fakefile_" + hashlib.sha256(
            (media_url or "").encode()).hexdigest()[:16]
        uploads.append({"url": media_url, "ref": ref, "at": time.time()})
        return MediaRef(
            ref=ref, size_bytes=1024 * 1024, mime_type=mime_type or "video/mp4",
            expires_at=datetime.now(timezone.utc) + timedelta(
                seconds=MEDIA_TTL_SECONDS),
        )

    async def submit(self, api_key: str, payload: PublishPayload) -> SubmitResult:
        if not api_key:
            raise ProviderError(E_AUTH, "no api key supplied")
        marker = _marker(payload)
        record = {
            "platform": plat.normalize(payload.platform),
            "account": payload.provider_account_ref,
            "caption": payload.caption,
            "media_ref": payload.media_ref,
            "marker": marker,
            "at": time.time(),
        }
        submissions.append(record)

        if marker == "fail-auth":
            raise ProviderError(E_AUTH, "fake: invalid credential",
                                status_code=401)
        if marker == "fail-blocked":
            raise ProviderError(E_NOT_CONNECTED,
                                "fake: destination not connected",
                                status_code=403)
        if marker == "fail-transient":
            raise ProviderError(E_NETWORK, "fake: transient submit failure")
        if marker == "fail-oversize":
            raise ProviderError(E_MEDIA_TOO_LARGE, "fake: media too large",
                                status_code=413)
        if marker == "fail-unknown":
            # The dangerous case: accepted-or-not, we cannot tell.
            raise ProviderError(E_UNKNOWN,
                                "fake: submit timed out with no response")
        if marker == "fail-ambiguous":
            # What actually happened in production: a gateway 5xx whose body is
            # not the provider's JSON. The post may already be live, so this is
            # ambiguous rather than retryable.
            raise ProviderError(
                E_UNKNOWN,
                "fake: HTTP 504 from an intermediary with no provider response "
                "body, so the post may or may not have been created",
                status_code=504,
                response={"_raw_text": "<html>Inactivity Timeout</html>"})

        post_ref = "fakepost_" + hashlib.sha256(
            f"{payload.provider_account_ref}{payload.caption}{len(submissions)}"
            .encode()).hexdigest()[:16]
        record["post_ref"] = post_ref

        if marker == "quota":
            # A 202: the daily cap is reached and the provider PARKED this post.
            # It exists on their side, so it is submitted-with-a-window, never
            # something to send again.
            return SubmitResult(
                status="submitted", provider_post_ref=post_ref,
                defer_seconds=3600,
                quota={"limit": 5, "remaining": 0,
                       "reset_at": datetime.now(timezone.utc) + timedelta(hours=1)},
                raw={"queued": True, "code": "queued_for_next_day"},
            )
        if marker == "refuse-capacity":
            # The other capacity shape: nothing was created, so the same payload
            # must be submitted again once the cooldown passes.
            record["post_ref"] = None
            return SubmitResult(
                status="deferred", defer_seconds=900,
                quota={"limit": 5, "remaining": 0},
                raw={"code": "rate_limited", "retry_after": 900},
            )
        if marker == "slow":
            return SubmitResult(
                status="submitted", provider_post_ref=post_ref,
                quota={"limit": 5, "remaining": 3},
                raw={"status": "pending"},
            )
        return SubmitResult(
            status="succeeded", provider_post_ref=post_ref,
            provider_native_post_ref="native_" + post_ref[-8:],
            permalink=f"https://example.invalid/{record['platform']}/{post_ref}",
            quota={"limit": 5, "remaining": 4},
            raw={"success": True, "status": "published"},
        )

    async def fetch_status(self, api_key: str,
                           provider_post_ref: str) -> Optional[SubmitResult]:
        # None, matching Status 200: the reconciler must exercise the
        # no-polling path in dry run too.
        return None

    def parse_webhook(self, payload: dict) -> WebhookEvent:
        data = payload.get("data") or {}
        return WebhookEvent(
            event_id=str(payload.get("id") or ""),
            event_type=str(payload.get("type") or "unknown"),
            provider_post_ref=str(data.get("post_id") or "") or None,
            provider_native_post_ref=str(data.get("platform_post_id") or "") or None,
            provider_account_ref=str(data.get("accountId") or "") or None,
            permalink=data.get("permalink"),
            error_message=data.get("error"),
            # The fake lets the caller choose the reading of a failure instead of
            # guessing from wording, so a dry run can exercise both the
            # definite-failure and the ambiguous branch on demand.
            error_code=data.get("error_code") or None,
            created_at=payload.get("created_at"),
            raw=payload,
        )

    async def verify_destination(self, api_key: str, platform: str,
                                 provider_account_ref: str) -> dict:
        if "fail-blocked" in (provider_account_ref or ""):
            return {"health": "blocked", "detail": "fake: not connected"}
        return {"health": "ok", "detail": "fake: destination reachable"}

    async def check_credential(self, api_key: str) -> dict:
        if not api_key or "bad" in api_key:
            return {"ok": False, "code": E_AUTH, "detail": "fake: rejected"}
        return {"ok": True, "code": None, "detail": "fake: key accepted"}


PROVIDER = FakeProvider()


def _register():
    from . import register
    register("fake", PROVIDER)


_register()
