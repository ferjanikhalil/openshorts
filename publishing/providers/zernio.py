"""Zernio adapter — the second provider.

Added because Status 200 is intermittently unavailable and a single provider is a
single point of failure for the whole publishing path. Contract verified against
Zernio's OpenAPI document and live probes (2026-08). Where it differs from
Status 200 the difference changes orchestration, not just wire format, so it is
recorded here rather than in a side document:

  * **The request body is FLAT, and that is a trap with a silent failure mode.**
    Status 200 wraps everything in ``{"post": {...}}``; Zernio does not. Worse,
    ``content`` is a plain string here and an object there. A wrapped body is not
    rejected — the validator simply finds no recognized fields, and the
    documented behaviour then applies: *"When none of scheduledFor, publishNow,
    or queuedFromProfile are provided, the post defaults to draft
    automatically."* So a body in Status 200's shape returns **201 Created** with
    ``status: "draft"``, and a draft never publishes. That is a successful HTTP
    call, a real post id, and no video on any platform. Two guards, because one
    is not enough: this adapter always sends ``publishNow`` or ``scheduledFor``,
    and it treats a response status of ``draft`` as a hard failure
    (``_assert_not_draft``) rather than a success.

  * **Remote scheduling is real**, unlike Status 200. ``scheduledFor`` +
    ``timezone`` returns ``status: "scheduled"`` with the timestamp echoed back,
    and ``DELETE /v1/posts/{id}`` cancels it. Hence
    ``supports_remote_schedule=True`` and ``supports_cancel_scheduled=True`` —
    the pair matters, because an uncancellable scheduled post is an
    uncontrollable one and the orchestrator refuses to hand the clock over
    without both. The consequence is the one the whole ``deploy/publisher``
    runbook exists to work around: for Zernio destinations nothing of ours needs
    to be awake when the slot arrives.

  * **Status lookup is real**: ``GET /v1/posts/{postId}``. Webhooks are still
    the fast path, but ``unknown`` stops being the only destination for a post
    whose callback got lost.

  * **There is no reusable media handle.** Zernio ingests
    ``mediaItems[].url`` per post, so ``supports_media_refs=False`` and the
    dispatcher presigns at the 7-day provider TTL instead of registering once.
    Zernio does expose a presigned-upload endpoint, deliberately unused: our
    bytes are already in the staging bucket, and pushing them a second time
    would send a full clip back up the same uplink the staging design exists to
    avoid.

  * **``X-RateLimit-*`` here is API throughput, NOT a post quota** — 60 requests
    per minute on the free tier, while *"posts themselves are unlimited on every
    connected social account"*. Status 200's headers carry the daily post cap and
    this adapter's look identical, so parsing them the same way would report
    "58 posts remaining today" as a publishing quota and let quota-aware dispatch
    make decisions on a number about HTTP calls. ``_parse_quota`` therefore reads
    those headers for nothing and quota stays empty unless a 429 body
    authoritatively says a *posting* limit was hit. Real per-account ceilings
    (50/day, 25/hour) are not exposed by any endpoint.

  * **403 carries a discriminator and the three cases need different humans.**
    ``code: ACCOUNT_DISCONNECTED`` is one account to re-link
    (``E_ACCOUNT_AUTH``); ``code: PROFILE_OVER_LIMIT`` is the free tier's
    2-accounts-per-Zernio-account ceiling (``E_PLAN_LIMIT`` — the reason this
    provider needs ``multi_credential``); no code at all means the ``accountId``
    does not belong to this key (``E_NOT_CONNECTED``). Status 200 cannot tell
    these apart at all, so this is a genuine improvement and worth reading the
    body for.

  * **A duplicate submit resolves instead of double-posting.** Two independent
    mechanisms, and both are load-bearing on the ambiguous-retry path: an
    ``x-request-id`` replay within ~5 minutes returns **200** with the original
    post under ``existingPost``, and a content-hash collision within 24 hours
    returns **409** with ``details.existingPostId``. Either way the post exists
    and its ref comes back, so an ambiguous retry ends up attached to the real
    post rather than creating a second one. ``_request_id`` is derived
    deterministically from the payload for exactly this reason.

  * **Per-platform failure detail is structured**, under ``platforms[i]``:
    ``errorCategory`` is a 10-value enum that maps cleanly onto ``errors.py``.
    One value needs care — ``unknown`` means *Zernio* does not know what
    happened, so it maps to ``E_UNKNOWN`` and is never auto-retried.

No credential is stored in this file. ``api_key`` is always passed in by the
caller, which decrypts it per submission.
"""
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .. import platforms as plat
from .. import signing
from ..errors import (
    E_ACCOUNT_AUTH, E_AUTH, E_DUPLICATE, E_MEDIA_TOO_LARGE,
    E_MEDIA_UNFETCHABLE, E_NETWORK, E_NOT_CONNECTED, E_PLAN_LIMIT,
    E_PROVIDER_5XX, E_QUOTA_EXHAUSTED, E_RATE_LIMITED, E_REMOTE_SCHEDULE,
    E_TIMEOUT, E_UNKNOWN, E_UNSUPPORTED, E_VALIDATION, ProviderError,
    classify_http_status,
)
from .base import (
    Capabilities, MediaRef, PublishPayload, SubmitResult, WebhookEvent,
)

BASE_URL = "https://zernio.com/api/v1"
POSTS_ENDPOINT = f"{BASE_URL}/posts"
ACCOUNTS_ENDPOINT = f"{BASE_URL}/accounts"

# Timestamps are sent as UTC with a literal Z, so the companion timezone field is
# UTC too. Sending a local zone name here while sending a UTC instant would shift
# every slot by the offset — silently, and only for operators outside UTC.
SCHEDULE_TIMEZONE = "UTC"

# Media URLs are handed over per submit; there is no ref to expire. Declared for
# symmetry with the media layer, which reads the capability rather than this.
MEDIA_URL_TTL_SECONDS = 7 * 24 * 3600

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 120.0

# Post-level lifecycle, from the spec's own enum.
S_DRAFT = "draft"
S_SCHEDULED = "scheduled"
S_PUBLISHING = "publishing"
S_PUBLISHED = "published"
S_FAILED = "failed"
S_PARTIAL = "partial"

# Statuses that mean the post is already on its way out. Used by the
# schedule_ignored detector: seeing one of these in answer to a FUTURE slot means
# the slot was dropped. Zernio honours schedules today — the detector is what
# notices the day that stops being true, after one post instead of a day's worth.
GOING_OUT_NOW = (S_PUBLISHING, S_PUBLISHED, S_PARTIAL)

CAPABILITIES = Capabilities(
    name="zernio",
    label="Zernio",
    key_prefix="sk_",
    platforms=(plat.YOUTUBE, plat.INSTAGRAM, plat.TIKTOK),
    # No reusable handle: media is ingested from mediaItems[].url on every post.
    supports_media_refs=False,
    media_by_url=True,
    media_ref_ttl_seconds=None,
    supports_status_lookup=True,
    # True on measurement, and only safe because cancel is True as well — see the
    # module docstring.
    supports_remote_schedule=True,
    supports_cancel_scheduled=True,
    supports_account_listing=True,
    supports_webhooks=True,
    signature_header="X-Zernio-Signature",
    one_platform_per_request=True,
    # The reason this adapter exists in its current shape: the free tier connects
    # 2 social accounts per Zernio account, and the usual fan-out needs 3. Several
    # Zernio keys therefore live in ONE publishing group, addressed by
    # credential_slot.
    multi_credential=True,
)

# Process-lifetime health of the remote-schedule field, kept even though Zernio
# honours it: the flag is what turns a silent accept-and-ignore into one logged
# event plus a fall back to the local clock.
_remote_schedule_ok = True


def remote_schedule_available() -> bool:
    return _remote_schedule_ok


def remote_schedule_disable(reason: str) -> None:
    global _remote_schedule_ok
    if _remote_schedule_ok:
        _remote_schedule_ok = False
        print("⚠️  Publishing: Zernio did not honour the scheduledFor field "
              f"({reason}); falling back to local-clock scheduling for this "
              "process.")


def _reset_remote_schedule() -> None:
    """Test seam. Never called in production."""
    global _remote_schedule_ok
    _remote_schedule_ok = True


def _iso_z(dt: datetime) -> str:
    """UTC, millisecond precision, literal ``Z`` — the shape the API echoes back.

    Same reasoning as the Status 200 adapter: ``datetime.isoformat()`` emits
    ``+00:00``, which a JS ``z.string().datetime()`` validator rejects unless the
    offset option is set. Here the field is required for a scheduled post so the
    failure would at least be loud, but ``scheduledFor`` is also what the echo
    comparison reads, and a format mismatch would make an honoured slot look
    dropped. Naive input is treated as UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def _post_obj(body: dict) -> dict:
    """The post object out of a response envelope.

    ``post`` on a normal create or a status lookup, ``existingPost`` on an
    ``x-request-id`` replay. Order matters only in that a replay response carries
    the latter; both are read so the caller does not have to know which path it
    took. Falls back to the body itself, so a future unwrapped response still
    parses.
    """
    for key in ("post", "existingPost", "data", "result"):
        inner = body.get(key)
        if isinstance(inner, dict):
            return inner
    return body


def _platform_entry(post: dict, platform: str) -> dict:
    """The ``platforms[]`` entry for one platform.

    One platform per request, so there is normally exactly one entry — but match
    on the name anyway rather than taking ``[0]``: reading another platform's
    ``failed`` status onto this attempt would fail a post that is perfectly live.
    """
    entries = post.get("platforms")
    if not isinstance(entries, list):
        return {}
    wanted = plat.normalize(platform)
    for entry in entries:
        if isinstance(entry, dict) and plat.normalize(
                str(entry.get("platform") or "")) == wanted:
            return entry
    if len(entries) == 1 and isinstance(entries[0], dict):
        return entries[0]
    return {}


def _headers(api_key: str, *, request_id: Optional[str] = None) -> dict:
    out = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "OpenShorts-Publishing/1",
    }
    if request_id:
        # Provider-side idempotency for ~5 minutes. See _request_id.
        out["x-request-id"] = request_id
    return out


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)


def _stable_media_key(media_url: Optional[str]) -> str:
    """A presigned URL's stable identity: everything except the signature.

    The query string of a presigned URL changes on every presign, so hashing the
    whole URL would give a different idempotency key to every retry of the same
    logical submit — which is precisely the case ``x-request-id`` exists to
    protect. Strip query and fragment and what is left is the object key.
    """
    if not media_url:
        return ""
    parts = urlsplit(media_url)
    return f"{parts.netloc}{parts.path}"


def _request_id(payload: PublishPayload) -> str:
    """Deterministic idempotency key for one logical submission.

    Derived rather than random, and that is the point: a retry after an ambiguous
    submit sends the SAME key, so within the provider's ~5 minute window the
    answer is 200 with the original post instead of a second real post. A random
    UUID would double-publish in exactly the case that matters.

    Two genuinely distinct submissions of the same clip, to the same account, at
    the same slot, within five minutes would collide. That is the intended
    trade: at our volume such a pair is an accidental double-click far more often
    than a deliberate republish, and the loser gets the winner's post ref rather
    than an error.
    """
    seed = "|".join([
        "openshorts-zernio-v1",
        plat.normalize(payload.platform),
        payload.provider_account_ref or "",
        _stable_media_key(payload.media_url or payload.media_ref),
        _iso_z(payload.scheduled_for) if payload.scheduled_for else "now",
        payload.caption or "",
        payload.title or "",
    ])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _parse_quota(resp_headers, body: dict) -> dict:
    """Posting quota — deliberately almost always empty.

    ``X-RateLimit-Limit/Remaining/Reset`` are present on every response and mean
    API requests per minute, not posts. Status 200's identically-named headers DO
    carry the daily post cap, so the tempting move is to share the parser; doing
    that would feed quota-aware dispatch a number about HTTP calls and have it
    conclude there are 58 posts left today. The headers are read for nothing.

    The only authoritative signal is a 429 (or a body) that names a *posting*
    limit, which ``_classify`` handles; this returns the parsed view for it.
    """
    out = {}

    def _int(v):
        try:
            return int(str(v).strip())
        except Exception:
            return None

    limit = _int(body.get("limit") or body.get("dailyLimit"))
    used = _int(body.get("used") or body.get("postsToday"))
    remaining = _int(body.get("remaining"))
    if limit is not None:
        out["limit"] = limit
    if remaining is not None:
        out["remaining"] = remaining
    elif limit is not None and used is not None:
        out["remaining"] = max(0, limit - used)

    reset = body.get("resetAt") or body.get("reset_at")
    reset_dt = _parse_dt(reset)
    if reset_dt:
        out["reset_at"] = reset_dt
    return out


def _quota_from_retry_after(resp_headers, body: dict) -> dict:
    """A posting limit we only learn about from a 429: remaining is 0 until reset."""
    out = _parse_quota(resp_headers, body)
    out["remaining"] = 0
    if "reset_at" not in out:
        delta = _retry_after_seconds(resp_headers) or 3600
        out["reset_at"] = datetime.now(timezone.utc) + timedelta(seconds=delta)
    return out


def _retry_after_seconds(resp_headers) -> Optional[int]:
    """``Retry-After`` in either RFC-9110 form: delta-seconds or an HTTP-date.

    Both forms have to work. Falling back to a fixed default on the date form
    would defer a several-hour posting cooldown by five minutes and re-burn it
    twelve times an hour — the header is the one authoritative number the
    provider gives us about when to come back.
    """
    raw = resp_headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(1, int(str(raw).strip()))
    except Exception:
        pass
    # HTTP-date form ("Mon, 24 Aug 2026 09:15:00 GMT"). Not ISO-8601, so
    # fromisoformat cannot read it; email.utils is the stdlib's HTTP-date parser.
    try:
        dt = parsedate_to_datetime(str(raw).strip())
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(1, int((dt - datetime.now(timezone.utc)).total_seconds()))


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
        return {"_raw_text": (resp.text or "")[:500]}


def _message(body: dict, default: str) -> str:
    """A human-readable failure message, with the offending FIELD kept.

    The headline and the field detail are combined rather than the first one
    winning. Zernio's validation errors are ``{"error": "Validation failed",
    "details": [<Zod issues>]}`` — the headline alone is the same seven words for
    a bad accountId, an over-long caption and a past ``scheduledFor``, so an
    operator reading only that has nothing to act on. The issue paths are the
    entire diagnostic value of the response.
    """
    headline = ""
    for key in ("error", "message", "detail", "code"):
        val = body.get(key)
        if isinstance(val, str) and val:
            headline = val
            break
        if isinstance(val, dict):
            inner = val.get("message") or val.get("detail") or val.get("error")
            if isinstance(inner, str) and inner:
                headline = inner
                break

    specifics = ""
    details = body.get("details")
    if isinstance(details, dict):
        inner = details.get("message") or details.get("reason")
        if isinstance(inner, str) and inner:
            specifics = inner
    elif isinstance(details, list) and details:
        # Zod issue array: join the first few "field: what is wrong" pairs.
        parts = []
        for item in details[:3]:
            if isinstance(item, dict):
                path = ".".join(str(p) for p in (item.get("path") or []))
                msg = item.get("message") or ""
                pair = f"{path}: {msg}".strip(": ")
                if pair:
                    parts.append(pair)
            elif isinstance(item, str) and item:
                parts.append(item)
        specifics = "; ".join(parts)

    if headline and specifics and specifics not in headline:
        return f"{headline} ({specifics})"
    return headline or specifics or default


def _is_provider_json(body: dict) -> bool:
    """Did Zernio's own handler write this body, or something in between?"""
    return bool(body) and "_raw_text" not in body


# Transport failures where the request provably never left this process.
_NEVER_SENT = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


def _submit_transport_error(exc: httpx.HTTPError) -> ProviderError:
    """Classify a submit that never produced a response.

    Identical reasoning to the Status 200 adapter, and it is not shared on
    purpose: the ambiguity rule is a property of *the submit call*, and inlining
    it keeps every adapter's riskiest branch readable in one file. If the request
    reached the provider — even partially — the post may exist, so a human
    resolves it rather than a retry creating a second post.
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


# Zernio's own per-platform error taxonomy, mapped onto ours. From the spec's
# errorCategory enum; errorSource (user | platform | system) is recorded in the
# message but does not change the classification, because the retry decision
# follows from the category alone.
_ERROR_CATEGORIES = {
    # The platform token behind ONE account died. Re-link that account; every
    # other destination in the group is fine.
    "auth_expired": E_ACCOUNT_AUTH,
    # The caption, media or options are not acceptable. Retrying the same bytes
    # cannot help.
    "user_content": E_VALIDATION,
    # Spam/abuse throttling aimed at the account.
    "user_abuse": E_RATE_LIMITED,
    "account_issue": E_NOT_CONNECTED,
    # The platform looked at it and said no. Permanent.
    "platform_rejected": E_UNSUPPORTED,
    "platform_error": E_PROVIDER_5XX,
    "platform_rate_limit": E_RATE_LIMITED,
    "quota_exhausted": E_QUOTA_EXHAUSTED,
    "system_error": E_PROVIDER_5XX,
    # Zernio does not know what happened. This is the one that must never be
    # auto-retried: the post may be live, and a retry on a live post
    # double-publishes.
    "unknown": E_UNKNOWN,
}


def _classify_platform_error(entry: dict) -> str:
    """Map a failed ``platforms[]`` entry to a taxonomy code."""
    category = str(entry.get("errorCategory") or "").strip().lower()
    code = _ERROR_CATEGORIES.get(category)
    if code:
        return code
    # No category, or one we have not seen. A failure we cannot classify is not
    # the same as a failure we know is permanent — but it is also not ambiguous
    # about whether the post went out, because the provider is telling us it did
    # not. Retryable provider error is the safe middle.
    return E_PROVIDER_5XX


def _platform_error_message(entry: dict, default: str) -> str:
    msg = entry.get("errorMessage") or entry.get("error") or default
    category = entry.get("errorCategory")
    source = entry.get("errorSource")
    suffix = " ".join(
        f"[{k}={v}]" for k, v in (("category", category), ("source", source))
        if v)
    return f"{msg} {suffix}".strip()


def _mentions_scheduled_for(resp: httpx.Response, body: dict) -> bool:
    """Did the provider refuse the FIELD rather than the post?

    Checked only on 4xx responses to a submit that carried ``scheduledFor``.
    Looks at the error body's field names — a Zod issue array echoes the
    offending path — and at the message text.
    """
    haystack = " ".join(str(k) for k in body.keys()) + " " + (resp.text or "")
    return "scheduledfor" in haystack.lower().replace("_", "").replace("-", "")


def _classify(resp: httpx.Response, body: dict, *,
              submit: bool = False) -> ProviderError:
    """Map an HTTP failure to the neutral taxonomy, body first.

    ``submit=True`` marks the one call that can create a real post, where an
    unattributable 5xx has to be ambiguous rather than retryable.
    """
    status = resp.status_code
    msg = _message(body, f"HTTP {status}")
    code = str(body.get("code") or "").strip().upper()
    lowered = f"{code} {msg}".lower()

    if status == 429:
        defer = _retry_after_seconds(resp.headers) or 300
        # Zernio returns 429 for two unrelated things: our API request rate (60
        # per minute, seconds to clear) and an account's posting velocity or
        # daily platform cap (hours). Deferring a throttle until tomorrow, or a
        # daily cap for 60 seconds, are both wrong — so read the body.
        if any(w in lowered for w in ("daily", "quota", "posts today",
                                      "per day", "cooldown")):
            err = ProviderError(E_QUOTA_EXHAUSTED, msg, status_code=status,
                                defer_seconds=max(defer, 900), response=body)
            err.response = {**body,
                            "_quota": _serialize_quota(
                                _quota_from_retry_after(resp.headers, body))}
            return err
        return ProviderError(E_RATE_LIMITED, msg, status_code=status,
                             defer_seconds=defer, response=body)

    if status == 409:
        # Content-hash duplicate within 24h. The caller handles the useful case
        # (an existing post ref, which resolves an ambiguous retry) before ever
        # reaching this; getting here means the response carried no ref, so the
        # post exists somewhere we cannot point at. Permanent — re-sending the
        # same content would only collide again.
        return ProviderError(E_DUPLICATE, msg, status_code=status,
                             response=body)

    if status == 403:
        # The one place Zernio is materially better than Status 200: the code
        # field says WHICH of three different humans has to do something.
        if code == "ACCOUNT_DISCONNECTED":
            return ProviderError(
                E_ACCOUNT_AUTH,
                f"{msg} — reconnect this account at Zernio.",
                status_code=status, response=body)
        if code == "PROFILE_OVER_LIMIT":
            return ProviderError(
                E_PLAN_LIMIT,
                f"{msg} — the Zernio plan's connected-account ceiling is "
                f"reached. Free tier allows 2 social accounts per Zernio "
                f"account; use an additional credential slot for the rest.",
                status_code=status, response=body)
        # No discriminator: the accountId is not owned by this key. A
        # configuration fact, so it marks the destination rather than retrying.
        return ProviderError(
            E_NOT_CONNECTED,
            f"{msg} — check the account reference for this destination (and "
            f"that it belongs to the credential slot this destination uses).",
            status_code=status, response=body)

    if status == 401:
        return ProviderError(E_AUTH, msg, status_code=status, response=body)
    if status == 402:
        return ProviderError(E_PLAN_LIMIT, msg, status_code=status,
                             response=body)
    if status == 413:
        return ProviderError(E_MEDIA_TOO_LARGE, msg, status_code=status,
                             response=body)
    if status == 422:
        return ProviderError(E_MEDIA_UNFETCHABLE, msg, status_code=status,
                             response=body)
    if status >= 500:
        if submit and not _is_provider_json(body):
            # An HTML/empty 5xx is an intermediary giving up on the RESPONSE,
            # not the provider refusing the REQUEST — their handler already ran
            # and may have created the post. This is the exact shape that
            # published a duplicate through the other provider on 2026-08-11.
            return ProviderError(
                E_UNKNOWN,
                f"HTTP {status} from an intermediary with no provider response "
                f"body, so the post may or may not have been created — check "
                f"the account before retrying",
                status_code=status, response=body)
        return ProviderError(E_PROVIDER_5XX, msg, status_code=status,
                             response=body)
    if 400 <= status < 500:
        return ProviderError(E_VALIDATION, msg, status_code=status,
                             response=body)
    return ProviderError(classify_http_status(status, body), msg,
                         status_code=status, response=body)


def _existing_post_ref(body: dict) -> Optional[str]:
    """The provider's own pointer at an already-created duplicate.

    Two shapes, both meaning "this post already exists, here it is":
      * 409 ``{"details": {"existingPostId": "..."}}`` — content-hash collision.
      * 200 ``{"existingPost": {"_id": ...}}`` — ``x-request-id`` replay.
    """
    details = body.get("details")
    if isinstance(details, dict):
        for key in ("existingPostId", "existing_post_id", "postId", "post_id"):
            val = details.get(key)
            if val:
                return str(val)
    existing = body.get("existingPost")
    if isinstance(existing, dict):
        ref = existing.get("_id") or existing.get("id")
        if ref:
            return str(ref)
    return None


def _platform_data(platform: str, payload: PublishPayload,
                   options: dict) -> dict:
    """Per-platform extras, with the defaults that are load-bearing.

    Only three defaults are set, and each one exists because leaving it unset has
    a known bad outcome. Everything else comes from the destination's own
    settings and is passed through untouched.
    """
    data = dict(options)
    if platform == plat.YOUTUBE:
        title = payload.title or data.get("title") or ""
        if title:
            limit = plat.TITLE_LIMITS.get(plat.YOUTUBE) or 100
            data["title"] = title[:limit]
        # YouTube requires an explicit audience declaration; a post that omits
        # it can be published and then have its views blocked. Default to "not
        # made for kids", which is what a clips channel is, and let the operator
        # override.
        data.setdefault("madeForKids", False)
        data.setdefault("visibility", "public")
    elif platform == plat.TIKTOK:
        # TikTok's native default is private/only-me. A private post never
        # surfaces the platform's public confirmation, so the provider stays
        # stuck reporting it as publishing forever.
        data.setdefault("privacyLevel", "PUBLIC_TO_EVERYONE")
        # NOT defaulted on purpose: contentPreviewConfirmed and
        # expressConsentGiven. Both appear in every TikTok example in Zernio's
        # docs, and TikTok may refuse a direct post without them — but they are
        # attestations that a human was shown a preview and consented. Setting
        # them here would make that claim to TikTok on the operator's behalf
        # about a UX flow that did not happen. They are settable per destination;
        # if TikTok rejects a post for their absence the error arrives as
        # platform_rejected with a message naming them.
    return data


class ZernioProvider:
    """Stateless adapter. One instance is shared; no credential is retained."""

    capabilities = CAPABILITIES

    # Webhook signature encoding is undocumented, so accept every standard one
    # rather than guessing. See signing.verify_webhook_signature_any_encoding —
    # it is constant-time across all candidates and has no early exit.
    verify_signature = staticmethod(
        signing.verify_webhook_signature_any_encoding)

    def remote_schedule_ok(self) -> bool:
        """Live health of the ``scheduledFor`` field for this process."""
        return remote_schedule_available()

    def disable_remote_schedule(self, reason: str) -> None:
        remote_schedule_disable(reason)

    async def upload_media(self, api_key: str, *, media_url: str,
                           mime_type: Optional[str] = None) -> MediaRef:
        """Not used, and the reason is a deliberate design choice.

        ``supports_media_refs=False`` means the dispatcher never calls this — it
        presigns a URL and passes it on the submit instead. Zernio does have a
        presigned-upload endpoint, but using it would push the whole clip from
        our staging bucket up to Zernio, a second full transfer of bytes the
        provider is perfectly willing to fetch itself. Avoiding exactly that is
        why the staging bucket exists (see the home-uplink measurement in
        media.py).

        Raising rather than silently succeeding: if the capability flag is ever
        flipped to True without implementing this, the failure should be loud and
        immediate rather than a stream of null refs.
        """
        raise ProviderError(
            E_UNSUPPORTED,
            "Zernio has no reusable media handle: media is ingested from "
            "mediaItems[].url on each post, so no pre-upload step is needed.")

    async def submit(self, api_key: str, payload: PublishPayload) -> SubmitResult:
        """Create one post for one platform on one account.

        One platform per request even though Zernio's ``platforms[]`` accepts a
        fan-out: a per-destination request maps 1:1 to a per-destination attempt
        row, so a partial failure is naturally attributable and the duplicate
        guard (a partial unique index on request+destination) means what it says.
        """
        platform = plat.normalize(payload.platform)
        media_url = payload.media_url or payload.media_ref
        if not media_url:
            raise ProviderError(E_VALIDATION, "no media url supplied")
        if not payload.provider_account_ref:
            raise ProviderError(
                E_VALIDATION,
                "no Zernio accountId for this destination; set the account "
                "reference in the admin UI.")

        body = {
            # A plain string here, NOT the object Status 200 takes. See the
            # module docstring: the wrong shape yields a silent 201 draft.
            "content": payload.caption or "",
            "platforms": [{
                "platform": platform,
                "accountId": payload.provider_account_ref,
            }],
            "mediaItems": [{
                # Declared explicitly rather than inferred from the extension:
                # a presigned URL carries a query string, and extension sniffing
                # on the far side would see ".mp4?X-Amz-Signature=..." — we only
                # ever publish clips, so this is always a video.
                "url": media_url,
                "type": "video",
            }],
            # Echoed back on the create response and on every webhook, so it is
            # the one place to leave a marker a human can recognize in Zernio's
            # own dashboard.
            "metadata": {"source": "openshorts", "platform": platform},
        }

        platform_data = _platform_data(platform, payload,
                                       dict(payload.options or {}))
        if platform_data:
            body["platforms"][0]["platformSpecificData"] = platform_data

        # EXACTLY ONE of these is always set, and that is not a stylistic choice:
        # with neither, the documented behaviour is "the post defaults to draft
        # automatically" — 201 Created, a real post id, and nothing ever
        # published.
        if payload.scheduled_for is not None:
            body["scheduledFor"] = _iso_z(payload.scheduled_for)
            body["timezone"] = SCHEDULE_TIMEZONE
        else:
            body["publishNow"] = True

        request_id = _request_id(payload)
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.post(
                    POSTS_ENDPOINT, json=body,
                    headers=_headers(api_key, request_id=request_id))
        except httpx.HTTPError as e:
            # CRITICAL: only a failure that provably never left this process is
            # retryable. Anything after the write is ambiguous — the post may
            # exist — and must not be retried automatically.
            raise _submit_transport_error(e) from e

        resp_body = _body(resp)

        if resp.status_code >= 400:
            # Before classifying: does the provider's refusal hand us the
            # existing post? A 409 content-hash collision does, and that turns
            # an ambiguous retry into a resolved attempt instead of either a
            # duplicate post or an unresolvable failure.
            existing = _existing_post_ref(resp_body)
            if resp.status_code == 409 and existing:
                return SubmitResult(
                    status="submitted",
                    provider_post_ref=existing,
                    quota=_parse_quota(resp.headers, resp_body),
                    raw={**resp_body, "_note": (
                        "provider reported this content as a duplicate and "
                        "returned the existing post; adopted its ref rather "
                        "than creating a second post")},
                )
            if (400 <= resp.status_code < 500
                    and payload.scheduled_for is not None
                    and _mentions_scheduled_for(resp, resp_body)):
                # The field itself was refused. A 4xx means no post was created,
                # so the caller may safely retry on the local clock.
                raise ProviderError(
                    E_REMOTE_SCHEDULE,
                    f"HTTP {resp.status_code}: the provider rejected the "
                    f"scheduledFor field: {_message(resp_body, 'unsupported')}",
                    status_code=resp.status_code, response=resp_body)
            raise _classify(resp, resp_body, submit=True)

        post = _post_obj(resp_body)
        post_ref = _first_str(post, "_id", "id", "postId", "post_id")
        provider_status = str(post.get("status") or "").strip().lower()
        entry = _platform_entry(post, platform)
        native_ref = _first_str(entry, "platformPostId", "platform_post_id",
                                "nativePostId")
        permalink = _first_str(entry, "publishedUrl", "published_url",
                              "permalink", "url")
        quota = _parse_quota(resp.headers, resp_body)

        # A 200 to a replayed x-request-id means the post already existed. Not a
        # failure, and not something to send again — adopt it.
        replayed = "existingPost" in resp_body

        self._assert_not_draft(provider_status, post_ref, resp, resp_body,
                               payload)

        # Did the provider actually take the slot? Only asked when we asked for
        # one. An echoed timestamp is positive proof the slot WAS taken and
        # overrides the status; absent that, a status meaning "already moving"
        # says it was dropped. Erring toward flagging costs one logged event and
        # a fall back to the local clock; erring the other way costs a whole
        # plan published at once.
        schedule_echo = post.get("scheduledFor") or post.get("scheduled_for")
        schedule_ignored = False
        if (payload.scheduled_for is not None and not schedule_echo
                and provider_status in GOING_OUT_NOW):
            schedule_ignored = True
            remote_schedule_disable(
                f"asked for {_iso_z(payload.scheduled_for)}, response says "
                f"{provider_status!r}: {_message(resp_body, 'no detail given')}")

        status, err = self._read_state(provider_status, entry, post_ref)
        if err is not None:
            err.status_code = resp.status_code
            err.response = resp_body
            raise err

        raw = dict(resp_body)
        if replayed:
            raw["_note"] = ("provider replayed a prior submission for this "
                            "x-request-id and returned the original post")
        return SubmitResult(
            status=status,
            provider_post_ref=post_ref,
            provider_native_post_ref=native_ref,
            permalink=permalink,
            quota=quota,
            raw=raw,
            schedule_ignored=schedule_ignored,
        )

    @staticmethod
    def _assert_not_draft(provider_status: str, post_ref: Optional[str],
                          resp: httpx.Response, resp_body: dict,
                          payload: PublishPayload) -> None:
        """A draft is a 201 that will never publish. Refuse to call it success.

        This is the adapter's most important guard. Zernio creates a draft
        whenever none of ``publishNow`` / ``scheduledFor`` / ``queuedFromProfile``
        is recognized, which is what a body in another provider's shape produces.
        ``submit`` always sends one of them, so reaching here means the field was
        sent and not understood — a contract change, not a caller mistake.

        Permanent, because retrying the same request produces another orphan
        draft. The ref is attached so a human can find and delete the one that
        already exists.
        """
        if provider_status != S_DRAFT:
            return
        raise ProviderError(
            E_VALIDATION,
            f"Zernio created a DRAFT instead of publishing: it did not honour "
            f"the "
            f"{'scheduledFor' if payload.scheduled_for else 'publishNow'} "
            f"field, so this post exists but will never go out. Delete draft "
            f"{post_ref or '(no id returned)'} at Zernio and check the API "
            f"contract before retrying.",
            status_code=resp.status_code, response=resp_body,
            provider_post_ref=post_ref)

    @staticmethod
    def _read_state(provider_status: str, entry: dict,
                    post_ref: Optional[str]):
        """Map (post status, platform entry) to our status or a ProviderError.

        Returns ``(status, None)`` or ``(None, ProviderError)``.

        The per-platform entry is authoritative wherever it exists, and the
        ordering is deliberate: one request carries one platform, so the entry
        describes THIS attempt while the post-level status describes the whole
        post. Those diverge in a way that matters — a post-level ``partial`` or
        even ``failed`` alongside an entry that published is a post where some
        OTHER platform failed, and reading the post level first would fail an
        attempt whose video is live.
        """
        entry_status = str(entry.get("status") or "").strip().lower()

        if entry_status:
            if entry_status == S_FAILED:
                code = _classify_platform_error(entry)
                msg = _platform_error_message(
                    entry, "the provider reported this post as failed")
                return None, ProviderError(code, msg,
                                           provider_post_ref=post_ref)
            if entry_status == S_PUBLISHED:
                return "succeeded", None
            # scheduled / publishing / anything unrecognized: pending.
            return "submitted", None

        # No per-platform entry at all (a bare create response). Fall back to the
        # post-level status.
        if provider_status == S_FAILED:
            code = _classify_platform_error(entry)
            msg = _platform_error_message(
                entry, "the provider reported this post as failed")
            return None, ProviderError(code, msg, provider_post_ref=post_ref)
        if provider_status == S_PUBLISHED:
            return "succeeded", None
        # 2xx with a status we do not recognize: accepted, outcome unclear.
        # Recorded as submitted rather than guessed as success.
        return "submitted", None

    async def fetch_status(self, api_key: str,
                           provider_post_ref: str) -> Optional[SubmitResult]:
        """Current state of a submitted post.

        Real here, unlike Status 200, which is what lets the reconciler resolve a
        post whose webhook was lost instead of aging it into ``unknown`` for a
        human.

        Returns None — "no information" — rather than raising, on both the
        unreachable case and a 404. A 404 is genuinely uninformative: a post
        deleted from Zernio's dashboard and a ref that was never valid look
        identical, and neither justifies declaring a post failed when it may be
        live on the platform. The stale sweeper's ``unknown`` is the right
        destination for both.
        """
        if not provider_post_ref:
            return None
        url = f"{POSTS_ENDPOINT}/{provider_post_ref}"
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.get(url, headers=_headers(api_key))
        except httpx.HTTPError:
            return None

        if resp.status_code == 404:
            return None
        body = _body(resp)
        if resp.status_code >= 400:
            # A lookup creates nothing, so submit=False: a 5xx here is plainly
            # retryable and the caller decides whether to care.
            raise _classify(resp, body)

        post = _post_obj(body)
        provider_status = str(post.get("status") or "").strip().lower()
        # ``[0]`` and not a name match, because the Protocol's fetch_status takes
        # only a ref — there is no platform to match against. Sound here because
        # this adapter always submits one platform per post, so a post it created
        # has exactly one entry. A hand-made multi-platform post looked up
        # through this method would report its first platform's state.
        entries = post.get("platforms")
        entry = entries[0] if isinstance(entries, list) and entries \
            and isinstance(entries[0], dict) else {}

        if provider_status == S_DRAFT:
            # Nothing to resolve: a draft is inert. Report it as pending rather
            # than succeeded so the sweeper eventually surfaces it.
            return SubmitResult(status="submitted",
                                provider_post_ref=str(provider_post_ref),
                                raw=body)

        status, err = self._read_state(provider_status, entry,
                                      str(provider_post_ref))
        if err is not None:
            err.status_code = resp.status_code
            err.response = body
            raise err
        return SubmitResult(
            status=status,
            provider_post_ref=str(provider_post_ref),
            provider_native_post_ref=_first_str(entry, "platformPostId",
                                                "platform_post_id"),
            permalink=_first_str(entry, "publishedUrl", "published_url",
                                 "permalink", "url"),
            raw=body,
        )

    async def cancel(self, api_key: str, provider_post_ref: str) -> bool:
        """Delete a scheduled post. Idempotent.

        This is what makes ``supports_remote_schedule=True`` safe: a slot handed
        to the provider can still be taken back. A 404 counts as success — the
        post is not there, which is the requested end state.
        """
        if not provider_post_ref:
            return False
        url = f"{POSTS_ENDPOINT}/{provider_post_ref}"
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.delete(url, headers=_headers(api_key))
        except httpx.HTTPError as e:
            raise ProviderError(
                E_NETWORK, f"could not reach the provider to cancel: {e}") from e
        if resp.status_code == 404 or 200 <= resp.status_code < 300:
            return True
        raise _classify(resp, _body(resp))

    def parse_webhook(self, payload: dict) -> WebhookEvent:
        """Normalize the ``{id, event, post, timestamp}`` envelope.

        Every field name differs from Status 200's, which is the whole reason
        ``parse_webhook`` is per-provider: ``event`` not ``type``, ``post`` not
        ``data``, ``publishedUrl`` not ``permalink``, ``timestamp`` not
        ``created_at``. Reading the other provider's names against this body
        yields a well-formed event with every field empty — accepted, acked, and
        matched to nothing.
        """
        post = payload.get("post")
        if not isinstance(post, dict):
            post = {}
        raw_event = str(payload.get("event") or payload.get("type") or "").lower()
        mapping = {
            "post.published": "post.published",
            "post.failed": "post.failed",
            "post.scheduled": "post.scheduled",
            # Per-platform variants of the same two outcomes. With one platform
            # per request they carry the same meaning for this attempt, and the
            # platform-level body is the one with the error detail.
            "post.platform.published": "post.published",
            "post.platform.failed": "post.failed",
            "account.disconnected": "account.disconnected",
            "account.reconnect_required": "account.disconnected",
        }
        event_type = mapping.get(raw_event, "unknown")

        # The platform entry is where the useful detail lives. Prefer one whose
        # status matches the event, so a partial fan-out webhook does not report
        # a sibling platform's success as this attempt's.
        entries = [e for e in (post.get("platforms") or [])
                   if isinstance(e, dict)]
        entry = {}
        if entries:
            want = S_FAILED if event_type == "post.failed" else S_PUBLISHED
            entry = next(
                (e for e in entries
                 if str(e.get("status") or "").lower() == want),
                entries[0])

        error_message = (_first_str(entry, "errorMessage", "error")
                         or _first_str(post, "errorMessage", "error", "message"))
        error_code = None
        if event_type == "post.failed":
            # post.platform.failed fires only on PERMANENT failures per the
            # spec, so a category is expected; when it is absent or reads
            # "unknown" the classification is E_UNKNOWN and nothing retries.
            error_code = _classify_platform_error(entry)

        return WebhookEvent(
            event_id=_first_str(payload, "id", "eventId", "event_id") or "",
            event_type=event_type,
            provider_post_ref=_first_str(post, "id", "_id", "postId"),
            provider_native_post_ref=_first_str(entry, "platformPostId",
                                                "platform_post_id"),
            provider_account_ref=_first_str(entry, "accountId", "account_id"),
            permalink=_first_str(entry, "publishedUrl", "published_url",
                                 "permalink", "url"),
            error_message=error_message,
            error_code=error_code,
            created_at=_epoch(payload.get("timestamp")
                              or payload.get("created_at")),
            raw=payload,
        )

    async def list_accounts(self, api_key: str) -> list:
        """Every social account connected to this credential.

        The capability Status 200 lacks entirely, and the one that makes the
        multi-credential setup tractable: an operator can see which of their
        Zernio accounts holds which social account instead of discovering it
        from a 403 on the first real post.
        """
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.get(ACCOUNTS_ENDPOINT,
                                        headers=_headers(api_key))
        except httpx.HTTPError as e:
            raise ProviderError(
                E_NETWORK, f"could not reach the provider: {e}") from e
        body = _body(resp)
        if resp.status_code >= 400:
            raise _classify(resp, body)
        return [_normalize_account(a) for a in (body.get("accounts") or [])
                if isinstance(a, dict)]

    async def verify_destination(self, api_key: str, platform: str,
                                 provider_account_ref: str) -> dict:
        """Prove a destination without publishing anything.

        Read-only: it lists the credential's accounts and looks for this one. A
        real check at setup time, which the other provider cannot offer — its
        destinations are only confirmed by their first real post.
        """
        try:
            accounts = await self.list_accounts(api_key)
        except ProviderError as e:
            if e.code == E_AUTH:
                return {"health": "blocked",
                        "detail": "Zernio rejected this API key (401)."}
            return {"health": "unverified",
                    "detail": f"could not list accounts ({e.code}): {e.message}"}

        wanted = plat.normalize(platform)
        match = next((a for a in accounts if a.get("ref") == str(
            provider_account_ref)), None)
        if match is None:
            same_platform = [a for a in accounts
                             if plat.normalize(a.get("platform") or "") == wanted]
            hint = ""
            if same_platform:
                hint = (" This credential does have "
                        + ", ".join(
                            f"{a.get('username') or a.get('ref')}"
                            for a in same_platform[:4])
                        + f" connected for {wanted}.")
            elif accounts:
                hint = (" This credential has no " + wanted + " account at all "
                        "— it may belong to a different credential slot.")
            return {"health": "blocked",
                    "detail": (f"no account {provider_account_ref!r} is "
                               f"connected to this credential.{hint}")}

        if plat.normalize(match.get("platform") or "") != wanted:
            return {"health": "blocked",
                    "detail": (f"that account is connected for "
                               f"{match.get('platform')}, not {wanted}.")}
        if match.get("needs_reconnection"):
            return {"health": "blocked",
                    "detail": (f"{match.get('username') or wanted} needs to be "
                               f"reconnected at Zernio before it can publish.")}
        if match.get("active") is False or match.get("enabled") is False:
            return {"health": "blocked",
                    "detail": (f"{match.get('username') or wanted} is connected "
                               f"but disabled at Zernio.")}
        return {"health": "ok",
                "detail": (f"connected as "
                           f"{match.get('username') or match.get('ref')}.")}

    async def check_credential(self, api_key: str) -> dict:
        """Is this API key accepted at all? Non-destructive by construction.

        A plain GET of the account list — no probe post, not even an invalid one,
        because this provider has a read endpoint and does not need the trick the
        Status 200 adapter uses.
        """
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.get(ACCOUNTS_ENDPOINT,
                                        headers=_headers(api_key))
        except httpx.HTTPError as e:
            return {"ok": False, "code": E_NETWORK,
                    "detail": f"could not reach the provider: {e}"}

        if resp.status_code == 401:
            return {"ok": False, "code": E_AUTH,
                    "detail": "Zernio rejected this API key (401)."}
        if resp.status_code == 402:
            return {"ok": False, "code": E_PLAN_LIMIT,
                    "detail": "the key authenticated but the plan refuses it "
                              "(402)."}
        if resp.status_code == 429:
            # Throughput, not a verdict on the key.
            return {"ok": True, "code": None,
                    "detail": "key accepted (rate limited on the check itself)."}
        if resp.status_code >= 500:
            return {"ok": False, "code": E_PROVIDER_5XX,
                    "detail": f"provider error {resp.status_code}; try again."}
        if resp.status_code >= 400:
            return {"ok": False, "code": E_VALIDATION,
                    "detail": _message(_body(resp), f"HTTP {resp.status_code}")}

        body = _body(resp)
        accounts = [a for a in (body.get("accounts") or []) if isinstance(a, dict)]
        if not accounts:
            # The key works; there is simply nothing connected behind it yet.
            # Not an error — a destination cannot be verified until the operator
            # links a social account in Zernio.
            return {"ok": True, "code": None,
                    "detail": ("key accepted, but no social accounts are "
                               "connected to it yet.")}
        names = ", ".join(
            f"{a.get('platform')}:{a.get('username') or a.get('displayName') or a.get('_id')}"
            for a in accounts[:6])
        return {"ok": True, "code": None,
                "detail": f"key accepted; {len(accounts)} account(s): {names}."}


def _normalize_account(acct: dict) -> dict:
    """One Zernio ``SocialAccount`` in the shape the admin UI expects."""
    return {
        "ref": _first_str(acct, "_id", "id", "accountId") or "",
        "platform": plat.normalize(str(acct.get("platform") or "")),
        "username": _first_str(acct, "username", "displayName", "name",
                               "handle"),
        "active": acct.get("isActive") if "isActive" in acct else None,
        "enabled": acct.get("enabled") if "enabled" in acct else None,
        "needs_reconnection": bool(acct.get("needsReconnection")),
        "raw": acct,
    }


def _serialize_quota(quota: dict) -> dict:
    out = dict(quota)
    reset = out.get("reset_at")
    if isinstance(reset, datetime):
        out["reset_at"] = reset.isoformat()
    return out


def _first_str(data: dict, *keys) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for k in keys:
        v = data.get(k)
        if isinstance(v, (str, int)) and str(v):
            return str(v)
    return None


def _epoch(value) -> Optional[float]:
    dt = _parse_dt(value)
    return dt.timestamp() if dt else None


PROVIDER = ZernioProvider()


def _register():
    from . import register
    register("zernio", PROVIDER)


_register()
