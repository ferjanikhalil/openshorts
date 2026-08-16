"""Status 200 Uploads adapter — the first provider.

Contract verified against the official documentation and, where the docs and the
live API disagree, against non-destructive unauthenticated probes (2026-08). The
disagreements are load-bearing, so they are recorded here rather than in a
side document:

  * **Endpoints.** The docs describe ``GET /v1/posts``, ``GET /v1/posts/{id}``,
    ``DELETE /v1/posts/{id}``, ``?action=profiles`` and ``?action=platforms``.
    Every one of those returned **405 Method Not Allowed** on probe. The two
    operations that do exist are ``POST /api/v2/posts`` and ``POST /api/v2/media``
    (both 401 unauthenticated, i.e. present and auth-gated).
    Consequence: ``supports_status_lookup=False``,
    ``supports_cancel_scheduled=False``, ``supports_account_listing=False``.
  * **CORS is not a method list.** ``api-posts`` advertises
    ``GET, POST, PUT, DELETE, OPTIONS``, but that is a permissive Supabase
    default; v2 correctly advertises only ``POST, OPTIONS``. The 405s are the
    real routing verdict.
  * **``accountId`` is the profile UUID**, despite the docs giving ``"@myprofile"``
    and the dashboard's own "⧉ API ID" button copying the handle. Established
    2026-08-11 by elimination: the handle in every casing, the per-platform
    ``social_media_connections`` UUID and the owning user UUID all return
    ``403 forbidden``; only the ``social_media_profiles`` row id gets past the
    ownership check. See ``provider_account_ref`` guidance in the admin UI.
  * **Their 403 cannot be read as "not yours".** A deliberately nonexistent
    account name returns byte-identical ``forbidden: Account does not belong to
    API key owner``, so the message conflates "unknown", "malformed" and "not
    permitted". A bogus *key* does return 401, which is the only way to tell a
    credential problem from a reference problem. Anything that surfaces this
    error to an operator has to say "check the reference" and not "check the key".
  * **Quota is per platform per account** (not per profile), free tier 5/day.
    At the target 3 posts/day/account that is 60% utilization — headroom of two,
    which retries can consume. Hence quota-aware dispatch is mandatory, not an
    optimization.
  * **Media is fetched by URL**, and ``file_id`` is reusable across posts AND
    platforms, so one upload serves a 3-platform fan-out. Refs roll off after
    7 days.
  * **A 202 is not an error.** ``queued_for_next_day`` means the daily cap is
    reached and the provider parked the post. It authoritatively sets remaining
    to 0 until the reset. A parked post EXISTS on their side, so it is recorded
    as ``submitted`` — never as something to send again.
  * **A 5xx on submit is not automatically retryable.** Observed 2026-08-11: one
    click produced two live posts because a gateway returned ``504`` with an HTML
    ``Inactivity Timeout`` page. The request bytes had already been sent and the
    post was created; classifying that as ``provider_error`` made it retryable
    and published a duplicate. A 5xx whose body is not the provider's own JSON
    did not come from the provider's handler, so on submit it is ambiguous.

No credential is stored in this file. ``api_key`` is always passed in by the
caller, which decrypts it per submission.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .. import platforms as plat
from ..errors import (
    E_AUTH, E_MEDIA_TOO_LARGE, E_MEDIA_UNFETCHABLE, E_NETWORK, E_NOT_CONNECTED,
    E_PROVIDER_5XX, E_QUOTA_EXHAUSTED, E_RATE_LIMITED, E_TIMEOUT, E_UNKNOWN,
    E_VALIDATION, ProviderError, classify_http_status,
)
from .base import (
    Capabilities, MediaRef, PublishPayload, SubmitResult, WebhookEvent,
)

BASE_URL = "https://status200uploads.com/api/v2"
MEDIA_ENDPOINT = f"{BASE_URL}/media"
POSTS_ENDPOINT = f"{BASE_URL}/posts"

# Provider media refs expire after 7 days.
MEDIA_TTL_SECONDS = 7 * 24 * 3600

# The provider requires a webhook ack within ~5s, so our own outbound calls get a
# generous but bounded timeout: a submit that hangs forever holds a worker slot
# and, worse, leaves an attempt in an ambiguous state.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 120.0

CAPABILITIES = Capabilities(
    name="status200",
    platforms=(plat.YOUTUBE, plat.INSTAGRAM, plat.TIKTOK),
    supports_media_refs=True,
    media_by_url=True,
    media_ref_ttl_seconds=MEDIA_TTL_SECONDS,
    # All three False by probe, not by assumption — see the module docstring.
    supports_status_lookup=False,
    supports_remote_schedule=False,
    supports_cancel_scheduled=False,
    supports_account_listing=False,
    supports_webhooks=True,
    one_platform_per_request=True,
)


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "OpenShorts-Publishing/1",
    }


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)


def _parse_quota(resp_headers, body: dict) -> dict:
    """Read the quota view from response headers, falling back to a 202 body.

    Headers appear on responses only — there is no quota query endpoint — so this
    is the sole source of truth, and a 202 body is the authoritative "you are out
    until the reset".
    """
    out = {}

    def _int(v):
        try:
            return int(str(v).strip())
        except Exception:
            return None

    limit = _int(resp_headers.get("X-RateLimit-Limit"))
    remaining = _int(resp_headers.get("X-RateLimit-Remaining"))
    reset = _int(resp_headers.get("X-RateLimit-Reset"))
    if limit is not None:
        out["limit"] = limit
    if remaining is not None:
        out["remaining"] = remaining
    if reset is not None:
        # Epoch seconds or a delta — both appear in the wild; treat a small
        # number as a delta.
        now = datetime.now(timezone.utc)
        out["reset_at"] = (now + timedelta(seconds=reset)) if reset < 10_000_000 \
            else datetime.fromtimestamp(reset, tz=timezone.utc)

    if body.get("queued") or body.get("code") == "queued_for_next_day":
        out["limit"] = body.get("limit", out.get("limit"))
        out["remaining"] = 0
        if body.get("scheduled_at"):
            out["reset_at"] = _parse_dt(body["scheduled_at"]) or out.get("reset_at")
    elif body.get("used") is not None and body.get("limit") is not None:
        used, limit_b = _int(body.get("used")), _int(body.get("limit"))
        if used is not None and limit_b is not None:
            out.setdefault("limit", limit_b)
            out.setdefault("remaining", max(0, limit_b - used))
    return out


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _body(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        # Keep a bounded slice: a provider HTML error page must not land whole in
        # the attempt's provider_response column.
        return {"_raw_text": (resp.text or "")[:500]}


def _message(body: dict, default: str) -> str:
    for key in ("message", "error", "detail", "code"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            inner = val.get("message") or val.get("detail")
            if isinstance(inner, str) and inner:
                return inner
    return default


# Transport failures where the request provably never left this process. Every
# other httpx error happened at or after the write, so on submit the post may
# already exist and the failure has to be treated as ambiguous.
_NEVER_SENT = (
    httpx.ConnectTimeout,      # the TCP/TLS handshake never completed
    httpx.ConnectError,        # DNS failure, refused, unreachable
    httpx.PoolTimeout,         # never even got a connection from our own pool
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,  # our own request was rejected before sending
)


def _is_provider_json(body: dict) -> bool:
    """Did the provider's own API write this body, or something in between?

    ``_body`` stores ``_raw_text`` when the response was not JSON, which is the
    tell for a proxy/CDN error page: the provider's API always answers JSON.
    """
    return bool(body) and "_raw_text" not in body


def _submit_transport_error(exc: httpx.HTTPError) -> ProviderError:
    """Classify a submit that never produced a response.

    The only question that matters is whether the request reached the provider.
    If it did — even partially — the post may exist, so the failure is ambiguous
    and a human resolves it rather than a retry creating a second post.
    """
    if isinstance(exc, _NEVER_SENT):
        return ProviderError(
            E_NETWORK,
            f"submit never reached the provider ({type(exc).__name__}): {exc}")
    return ProviderError(
        E_UNKNOWN,
        f"submit failed after the request was sent "
        f"({type(exc).__name__}: {exc}); the post may or may not have been "
        f"created — check the account before retrying")


def _classify(resp: httpx.Response, body: dict, *,
              submit: bool = False) -> ProviderError:
    """Map an HTTP failure to the neutral taxonomy, body first.

    ``submit=True`` marks the one call that can create a real post, where an
    unattributable 5xx has to be ambiguous rather than retryable.
    """
    status = resp.status_code
    msg = _message(body, f"HTTP {status}")
    code = body.get("code") or ""
    lowered = f"{code} {msg}".lower()

    if status == 429:
        # 429 is the spacing cooldown, NOT the daily cap. Conflating them would
        # park a post until midnight over a few seconds of throttling.
        retry_after = resp.headers.get("Retry-After")
        try:
            defer = int(retry_after) if retry_after else 300
        except Exception:
            defer = 300
        if "daily" in lowered or "quota" in lowered:
            return ProviderError(E_QUOTA_EXHAUSTED, msg, status_code=status,
                                 defer_seconds=max(defer, 900), response=body)
        return ProviderError(E_RATE_LIMITED, msg, status_code=status,
                             defer_seconds=defer, response=body)

    if status == 403:
        # 403 means the destination is not connected to this credential — a
        # configuration fact, so it marks the destination rather than retrying.
        return ProviderError(E_NOT_CONNECTED, msg, status_code=status,
                             response=body)
    if status == 401:
        return ProviderError(E_AUTH, msg, status_code=status, response=body)
    if status == 413:
        return ProviderError(E_MEDIA_TOO_LARGE, msg, status_code=status,
                             response=body)
    if status == 422:
        return ProviderError(E_MEDIA_UNFETCHABLE, msg, status_code=status,
                             response=body)
    if status >= 500:
        if submit and not _is_provider_json(body):
            # An HTML/empty 5xx is a gateway giving up on the RESPONSE, not the
            # provider refusing the REQUEST — their handler already ran and may
            # have created the post. Retrying this is what published a duplicate
            # on 2026-08-11.
            return ProviderError(
                E_UNKNOWN,
                f"HTTP {status} from an intermediary with no provider response "
                f"body, so the post may or may not have been created — check "
                f"the account before retrying",
                status_code=status, response=body)
        # A structured error body means the provider's own handler took its
        # error path deliberately, which is safely retryable.
        return ProviderError(E_PROVIDER_5XX, msg, status_code=status,
                             response=body)
    if 400 <= status < 500:
        return ProviderError(E_VALIDATION, msg, status_code=status, response=body)
    return ProviderError(classify_http_status(status, body), msg,
                         status_code=status, response=body)


class Status200Provider:
    """Stateless adapter. One instance is shared; no credential is retained."""

    capabilities = CAPABILITIES

    async def upload_media(self, api_key: str, *, media_url: str,
                           mime_type: Optional[str] = None) -> MediaRef:
        """Register media by public URL. Returns a ref reusable across platforms.

        The endpoint takes a single field, ``url`` — the provider downloads the
        bytes itself, which is why publishing needs a publicly reachable media
        origin (see media.py).
        """
        payload = {"url": media_url}
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.post(MEDIA_ENDPOINT, json=payload,
                                         headers=_headers(api_key))
        except httpx.TimeoutException as e:
            # A media upload timeout is safely retryable: no post was created,
            # so at worst we re-register the same bytes.
            raise ProviderError(E_TIMEOUT, f"media upload timed out: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderError(E_NETWORK, f"media upload failed: {e}") from e

        body = _body(resp)
        if resp.status_code >= 400:
            raise _classify(resp, body)

        ref = body.get("file_id") or body.get("fileId") or body.get("id")
        if not ref:
            raise ProviderError(
                E_VALIDATION,
                f"media upload returned no file_id: {_message(body, 'no id')}",
                status_code=resp.status_code, response=body)
        return MediaRef(
            ref=str(ref),
            size_bytes=body.get("size"),
            mime_type=body.get("type") or mime_type,
            expires_at=datetime.now(timezone.utc) + timedelta(
                seconds=MEDIA_TTL_SECONDS),
        )

    async def submit(self, api_key: str, payload: PublishPayload) -> SubmitResult:
        """Create one post for one platform on one account.

        One platform per request is the provider's shape, and it happens to be
        exactly what we want: a per-destination request maps 1:1 to a
        per-destination attempt row, so a partial fan-out failure is naturally
        attributable.
        """
        platform = plat.normalize(payload.platform)
        content = {"text": payload.caption or ""}
        if payload.media_ref:
            content["mediaID"] = [payload.media_ref]
        elif payload.media_url:
            content["mediaUrls"] = [payload.media_url]
        else:
            raise ProviderError(E_VALIDATION,
                                "no media ref or media url supplied")

        post = {
            "accountId": payload.provider_account_ref,
            "platform": platform,
            "content": content,
        }
        # Remote scheduling is unsupported (no cancel endpoint), so
        # scheduled_for is deliberately NOT forwarded — the dispatcher holds the
        # clock locally and submits at the appointed time. Sending it would create
        # a post we could not recall.
        options = dict(payload.options or {})
        if payload.title and platform == plat.YOUTUBE:
            options.setdefault("title", payload.title)
        if platform == plat.TIKTOK:
            # TikTok's native default is "moi uniquement" (private/only-me). A
            # private post never surfaces the platform's public confirmation, so
            # the provider stays stuck reporting the post as processing. Post
            # PUBLIC unless the operator explicitly chose another visibility.
            options.setdefault("privacyStatus", "public")
        if options:
            post[platform] = options

        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.post(POSTS_ENDPOINT, json={"post": post},
                                         headers=_headers(api_key))
        except httpx.HTTPError as e:
            # CRITICAL: only a failure that provably never left this process is
            # retryable here. Anything after the write is ambiguous — the
            # provider may have accepted and published — and must NOT be retried
            # automatically. See _submit_transport_error.
            raise _submit_transport_error(e) from e

        body = _body(resp)
        quota = _parse_quota(resp.headers, body)

        if resp.status_code == 202 or body.get("queued"):
            # Daily cap reached: the provider parked the post for the next
            # window. The post EXISTS on their side, so this is `submitted`, not
            # `deferred` — deferring would re-submit at timer expiry and publish
            # a second copy. `defer_seconds` only tells the sweeper how long to
            # wait before calling the silence suspicious.
            scheduled_at = _parse_dt(body.get("scheduled_at"))
            defer = 3600
            if scheduled_at:
                delta = (scheduled_at - datetime.now(timezone.utc)).total_seconds()
                defer = int(max(300, min(delta, 26 * 3600)))
            ref = (body.get("scheduled_post_id") or body.get("post_id")
                   or body.get("postId") or body.get("id"))
            return SubmitResult(
                status="submitted",
                provider_post_ref=str(ref) if ref else None,
                defer_seconds=defer,
                quota=quota,
                raw=body,
            )

        if resp.status_code >= 400:
            err = _classify(resp, body, submit=True)
            if err.code == E_QUOTA_EXHAUSTED:
                err.response = {**body, "_quota": _serialize_quota(quota)}
            raise err

        post_ref = (body.get("post_id") or body.get("postId")
                    or body.get("id") or body.get("scheduled_post_id"))
        provider_status = str(body.get("status") or "").lower()
        native_ref = (body.get("platform_post_id") or body.get("native_post_id")
                      or body.get("platformPostId"))

        if provider_status == "published":
            status = "succeeded"
        elif provider_status in ("scheduled", "pending", "queued", "processing"):
            # Accepted but not live. A webhook (or the stale sweeper) resolves it.
            status = "submitted"
        elif provider_status == "failed":
            raise ProviderError(
                E_VALIDATION, _message(body, "provider reported failed"),
                status_code=resp.status_code, response=body,
                provider_post_ref=str(post_ref) if post_ref else None)
        else:
            # 2xx with an unrecognized status: accepted, outcome unclear. Record
            # as submitted rather than guessing success.
            status = "submitted"

        return SubmitResult(
            status=status,
            provider_post_ref=str(post_ref) if post_ref else None,
            provider_native_post_ref=str(native_ref) if native_ref else None,
            permalink=body.get("permalink") or body.get("url"),
            quota=quota,
            raw=body,
        )

    async def fetch_status(self, api_key: str,
                           provider_post_ref: str) -> Optional[SubmitResult]:
        """Unsupported: every documented lookup route returned 405 on probe.

        Returning None (rather than raising) lets the reconciler treat "no
        polling available" as a normal condition and fall back to the stale
        sweeper. If the provider ships a lookup endpoint later, this method and
        ``supports_status_lookup`` are the only things that change.
        """
        return None

    def parse_webhook(self, payload: dict) -> WebhookEvent:
        """Normalize the ``{id, type, created_at, data}`` envelope."""
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        raw_type = str(payload.get("type") or "").lower()
        mapping = {
            "post.published": "post.published",
            "post.failed": "post.failed",
            "post.scheduled": "post.scheduled",
            "profile.disconnected": "account.disconnected",
        }
        event_type = mapping.get(raw_type, "unknown")
        error_message = _first_str(data, "error", "message", "reason")
        return WebhookEvent(
            event_id=str(payload.get("id") or ""),
            event_type=event_type,
            provider_post_ref=_first_str(data, "post_id", "postId", "id",
                                         "scheduled_post_id"),
            provider_native_post_ref=_first_str(data, "platform_post_id",
                                                "native_post_id",
                                                "platformPostId"),
            provider_account_ref=_first_str(data, "accountId", "account_id",
                                            "profile", "profile_username"),
            permalink=_first_str(data, "permalink", "url", "post_url"),
            error_message=error_message,
            error_code=(_classify_failure_reason(error_message)
                        if event_type == "post.failed" else None),
            created_at=_epoch(payload.get("created_at")),
            raw=payload,
        )

    async def verify_destination(self, api_key: str, platform: str,
                                 provider_account_ref: str) -> dict:
        """Cannot be verified without publishing.

        There is no account-listing endpoint (405 on probe) and no dry-run mode,
        so the only way to prove a destination is reachable is to post to it.
        Doing that silently during setup would put real content on a real
        audience, so this reports ``unverified`` and the health is instead proven
        by the first real publish (a 403 flips the destination to ``blocked``).

        The one thing that IS checkable is whether the credential itself works,
        which is done in ``check_credential``.
        """
        return {
            "health": "unverified",
            "detail": ("Status 200 exposes no account-listing or dry-run "
                       "endpoint, so this destination is confirmed by its first "
                       "real publish."),
        }

    async def check_credential(self, api_key: str) -> dict:
        """Is this API key accepted at all?

        Deliberately non-destructive: it posts an intentionally invalid media
        registration. A 401 proves the key is bad; anything else (400/422 for the
        bad URL) proves the key authenticated, since auth is evaluated before
        request validation on this endpoint — which the probes confirmed.
        """
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.post(
                    MEDIA_ENDPOINT,
                    json={"url": "https://openshorts.invalid/credential-check"},
                    headers=_headers(api_key))
        except httpx.HTTPError as e:
            return {"ok": False, "code": E_NETWORK,
                    "detail": f"could not reach the provider: {e}"}

        if resp.status_code == 401:
            return {"ok": False, "code": E_AUTH,
                    "detail": "the provider rejected this API key (401)."}
        if resp.status_code == 403:
            return {"ok": False, "code": E_NOT_CONNECTED,
                    "detail": "the key authenticated but is not authorized (403)."}
        if resp.status_code >= 500:
            return {"ok": False, "code": E_PROVIDER_5XX,
                    "detail": f"provider error {resp.status_code}; try again."}
        # 2xx/4xx other than 401/403: authentication passed.
        return {"ok": True, "code": None,
                "detail": f"key accepted (probe returned {resp.status_code})."}


def _serialize_quota(quota: dict) -> dict:
    out = dict(quota)
    reset = out.get("reset_at")
    if isinstance(reset, datetime):
        out["reset_at"] = reset.isoformat()
    return out


# Wording Status 200 uses when it gave up WAITING rather than learned of a
# failure. Observed 2026-08-11: a clip that reached TikTok was reported "Timeout"
# because their integration never received the platform's publish confirmation.
# A post they never confirmed either way is not a post we may safely re-send.
_AMBIGUOUS_FAILURE_WORDS = (
    "timeout", "timed out", "no response", "no confirmation", "took too long",
    "unknown", "pending",
)


def _classify_failure_reason(message: Optional[str]) -> Optional[str]:
    """Map a ``post.failed`` reason to E_UNKNOWN when it means "we never learned".

    Returning None means "no opinion" — the caller keeps its default of a
    retryable provider error, which is right for a definite refusal.
    """
    lowered = (message or "").lower()
    if any(w in lowered for w in _AMBIGUOUS_FAILURE_WORDS):
        return E_UNKNOWN
    return None


def _first_str(data: dict, *keys) -> Optional[str]:
    for k in keys:
        v = data.get(k)
        if isinstance(v, (str, int)) and str(v):
            return str(v)
    return None


def _epoch(value) -> Optional[float]:
    dt = _parse_dt(value)
    return dt.timestamp() if dt else None


PROVIDER = Status200Provider()


def _register():
    from . import register
    register("status200", PROVIDER)


_register()
