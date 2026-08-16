"""Contract tests for the provider layer.

The Status 200 adapter is exercised against `httpx.MockTransport` fixtures built
from the documented (and probe-verified) wire shapes — no network, no credential,
no real post. The adapter builds its client inline, so the seam here is a
subclass of `httpx.AsyncClient` that injects the mock transport; the adapter
source is untouched by the test.

Coroutines are driven with `asyncio.run` from sync test functions because CI
installs no pytest-asyncio and the repo has no async-test convention.

Every api_key below is a made-up `rl_test_…` string. Nothing here can reach a
real provider: the transport never opens a socket.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from publishing import errors, platforms as plat, providers
from publishing.providers import fake, status200
from publishing.providers.base import PublishPayload

API_KEY = "rl_test_0000111122223333"


def run(coro):
    return asyncio.run(coro)


# Captured once, before any monkeypatching. Subclassing the *live*
# httpx.AsyncClient would nest a previous test's transport underneath the new one
# and the older handler would win.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_client(handler):
    """An AsyncClient subclass that answers from `handler` instead of the network."""

    class _MockedClient(_REAL_ASYNC_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockedClient


def install(monkeypatch, handler):
    """Point the adapter's httpx at `handler`; return the captured request list."""
    seen = []

    def _capture(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(status200.httpx, "AsyncClient", mock_client(_capture))
    return seen


def body_of(request):
    return json.loads(request.content.decode())


def json_route(status, payload, headers=None):
    def handler(request):
        return httpx.Response(status, json=payload, headers=headers or {})
    return handler


def raw_route(status, body_bytes, headers=None):
    """A non-JSON response — what a proxy/CDN error page looks like on the wire.

    The adapter's `_body` stores `_raw_text` for these, which is the tell that an
    intermediary (not the provider's API) produced it.
    """
    def handler(request):
        return httpx.Response(status, content=body_bytes,
                              headers=headers or {"content-type": "text/html"})
    return handler


def payload(**over):
    base = dict(platform=plat.INSTAGRAM, provider_account_ref="acct_123",
                caption="a clip", media_ref="file_abc")
    base.update(over)
    return PublishPayload(**base)


# --------------------------------------------------------------------------
# Media registration
# --------------------------------------------------------------------------
class TestUploadMedia:
    def test_registers_by_url_and_returns_a_reusable_ref(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, {
            "success": True, "file_id": "f_9911", "size": 4194304,
            "type": "video/mp4"}))

        ref = run(status200.PROVIDER.upload_media(
            API_KEY, media_url="https://cdn.example.test/clip.mp4"))

        assert ref.ref == "f_9911"
        assert ref.size_bytes == 4194304
        assert ref.mime_type == "video/mp4"
        # 7-day TTL — the refresh margin depends on this being populated.
        assert ref.expires_at is not None
        remaining = (ref.expires_at - datetime.now(timezone.utc)).total_seconds()
        assert abs(remaining - status200.MEDIA_TTL_SECONDS) < 60

        req = seen[0]
        assert str(req.url) == status200.MEDIA_ENDPOINT
        # The endpoint takes exactly one field: the provider fetches the bytes.
        assert body_of(req) == {"url": "https://cdn.example.test/clip.mp4"}
        assert req.headers["Authorization"] == f"Bearer {API_KEY}"

    def test_accepts_the_camelCase_and_bare_id_spellings(self, monkeypatch):
        install(monkeypatch, json_route(200, {"fileId": "f_camel"}))
        assert run(status200.PROVIDER.upload_media(
            API_KEY, media_url="https://x.test/a.mp4")).ref == "f_camel"

        install(monkeypatch, json_route(200, {"id": "f_bare"}))
        assert run(status200.PROVIDER.upload_media(
            API_KEY, media_url="https://x.test/a.mp4")).ref == "f_bare"

    def test_a_2xx_with_no_ref_is_a_validation_error(self, monkeypatch):
        # Silently returning a MediaRef(ref=None) would submit a post with no
        # media attached.
        install(monkeypatch, json_route(200, {"success": True}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.upload_media(
                API_KEY, media_url="https://x.test/a.mp4"))
        assert exc.value.code == errors.E_VALIDATION

    def test_oversize_is_permanent(self, monkeypatch):
        install(monkeypatch, json_route(413, {"message": "file too large"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.upload_media(
                API_KEY, media_url="https://x.test/a.mp4"))
        assert exc.value.code == errors.E_MEDIA_TOO_LARGE
        assert exc.value.retryable is False

    def test_unfetchable_url_is_permanent(self, monkeypatch):
        install(monkeypatch, json_route(422, {"message": "could not fetch url"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.upload_media(
                API_KEY, media_url="http://localhost:8000/clip.mp4"))
        assert exc.value.code == errors.E_MEDIA_UNFETCHABLE

    def test_upload_timeout_is_retryable(self, monkeypatch):
        # No post exists yet, so re-registering the same bytes is harmless. This
        # is the deliberate asymmetry with submit() below.
        def handler(request):
            raise httpx.ReadTimeout("read timed out", request=request)

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.upload_media(
                API_KEY, media_url="https://x.test/a.mp4"))
        assert exc.value.code == errors.E_TIMEOUT
        assert exc.value.retryable is True
        assert exc.value.is_ambiguous is False

    def test_connect_error_is_retryable(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("no route to host", request=request)

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.upload_media(
                API_KEY, media_url="https://x.test/a.mp4"))
        assert exc.value.code == errors.E_NETWORK
        assert exc.value.retryable is True

    def test_an_html_error_page_is_truncated_not_stored_whole(self, monkeypatch):
        def handler(request):
            return httpx.Response(500, text="<html>" + "x" * 5000 + "</html>")

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.upload_media(
                API_KEY, media_url="https://x.test/a.mp4"))
        assert exc.value.code == errors.E_PROVIDER_5XX
        assert len(exc.value.response.get("_raw_text", "")) <= 500


# --------------------------------------------------------------------------
# Submit — request shape
# --------------------------------------------------------------------------
class TestSubmitRequestShape:
    def test_one_platform_per_request_with_a_media_ref(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"success": True, "status": "published", "post_id": "p_1"}))

        run(status200.PROVIDER.submit(API_KEY, payload()))

        sent = body_of(seen[0])
        assert str(seen[0].url) == status200.POSTS_ENDPOINT
        assert sent == {"post": {
            "accountId": "acct_123",
            "platform": "instagram",
            "content": {"text": "a clip", "mediaID": ["file_abc"]},
        }}

    def test_falls_back_to_a_media_url_when_there_is_no_ref(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(
            media_ref=None, media_url="https://cdn.example.test/clip.mp4")))
        content = body_of(seen[0])["post"]["content"]
        assert content["mediaUrls"] == ["https://cdn.example.test/clip.mp4"]
        assert "mediaID" not in content

    def test_no_media_at_all_never_reaches_the_provider(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, {}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(
                API_KEY, payload(media_ref=None, media_url=None)))
        assert exc.value.code == errors.E_VALIDATION
        assert seen == []

    def test_platform_aliases_are_normalized_on_the_wire(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(platform="reels")))
        assert body_of(seen[0])["post"]["platform"] == "instagram"

    def test_title_is_sent_for_youtube_only(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(
            platform=plat.YOUTUBE, title="How to ship on Friday")))
        assert body_of(seen[0])["post"]["youtube"]["title"] == \
            "How to ship on Friday"

        seen.clear()
        run(status200.PROVIDER.submit(API_KEY, payload(
            platform=plat.TIKTOK, title="How to ship on Friday")))
        sent = body_of(seen[0])["post"]
        # The title is never forwarded to TikTok — only the platform default
        # visibility (public) is set.
        assert "title" not in json.dumps(sent)
        assert sent["tiktok"] == {"privacyStatus": "public"}

    def test_tiktok_defaults_to_public_privacy(self, monkeypatch):
        # Load-bearing: TikTok's native default is "moi uniquement" (private).
        # A private post never produces the platform's public confirmation, so
        # Status 200 stays stuck reporting the post as `processing`. Every
        # TikTok submission must default to public unless the operator has
        # explicitly chosen another visibility.
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(platform=plat.TIKTOK)))
        sent = body_of(seen[0])["post"]
        assert sent["tiktok"]["privacyStatus"] == "public"

    def test_tiktok_explicit_privacy_still_wins(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(
            platform=plat.TIKTOK, options={"privacyStatus": "private"})))
        sent = body_of(seen[0])["post"]
        assert sent["tiktok"]["privacyStatus"] == "private"

    def test_tiktok_public_default_does_not_leak_to_other_platforms(
            self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(
            platform=plat.INSTAGRAM)))
        sent = body_of(seen[0])["post"]
        assert "privacyStatus" not in json.dumps(sent)
        assert "instagram" not in sent

    def test_per_platform_options_pass_through(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        run(status200.PROVIDER.submit(API_KEY, payload(
            platform=plat.YOUTUBE, options={"privacyStatus": "private"})))
        assert body_of(seen[0])["post"]["youtube"]["privacyStatus"] == "private"

    def test_scheduled_for_is_never_forwarded(self, monkeypatch):
        # Load-bearing: there is no working cancel endpoint, so a remotely
        # scheduled post would be unrecallable. The dispatcher holds the clock.
        seen = install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"}))
        when = datetime.now(timezone.utc) + timedelta(hours=6)
        run(status200.PROVIDER.submit(API_KEY, payload(scheduled_for=when)))

        raw = seen[0].content.decode()
        assert "scheduled" not in raw.lower()
        assert str(when.year) not in raw


# --------------------------------------------------------------------------
# Submit — response handling
# --------------------------------------------------------------------------
class TestSubmitResponses:
    def test_published_is_succeeded(self, monkeypatch):
        install(monkeypatch, json_route(200, {
            "success": True, "status": "published", "post_id": "p_42",
            "platform_post_id": "IG_998", "permalink": "https://ig.test/p/998"}))
        res = run(status200.PROVIDER.submit(API_KEY, payload()))
        assert res.status == "succeeded"
        assert res.provider_post_ref == "p_42"
        assert res.provider_native_post_ref == "IG_998"
        assert res.permalink == "https://ig.test/p/998"

    @pytest.mark.parametrize("provider_status",
                             ["scheduled", "pending", "queued", "processing"])
    def test_accepted_but_not_live_is_submitted(self, monkeypatch,
                                                provider_status):
        install(monkeypatch, json_route(
            200, {"status": provider_status, "post_id": "p_7"}))
        res = run(status200.PROVIDER.submit(API_KEY, payload()))
        assert res.status == "submitted"
        assert res.provider_post_ref == "p_7"

    def test_an_unrecognized_status_is_submitted_not_guessed_successful(
            self, monkeypatch):
        install(monkeypatch, json_route(
            200, {"status": "something_new", "post_id": "p_8"}))
        assert run(status200.PROVIDER.submit(
            API_KEY, payload())).status == "submitted"

    def test_a_2xx_reporting_failed_raises(self, monkeypatch):
        # A 200 body that says "failed" is still a failure, and the post ref it
        # carries is the only handle a human has to check.
        install(monkeypatch, json_route(200, {
            "status": "failed", "post_id": "p_9",
            "message": "instagram rejected the aspect ratio"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_VALIDATION
        assert exc.value.provider_post_ref == "p_9"
        assert "aspect ratio" in exc.value.message

    def test_202_queued_for_next_day_is_submitted_parked_not_failed(self, monkeypatch):
        install(monkeypatch, json_route(202, {
            "queued": True, "code": "queued_for_next_day",
            "scheduled_post_id": "sp_5", "limit": 5}))
        res = run(status200.PROVIDER.submit(API_KEY, payload()))
        # A 202 means the provider PARKED the post — it exists on their side, so
        # re-submitting it (what "deferred" would do) would publish a duplicate.
        # It is submitted-with-a-window: held as live, waiting on confirmation.
        assert res.status == "submitted"
        assert res.provider_post_ref == "sp_5"
        # Authoritative: the daily cap is spent until the reset.
        assert res.quota["remaining"] == 0
        assert res.quota["limit"] == 5
        # defer_seconds is now "how long silence is still normal", not "send
        # it again after this" — but it is still set so the sweeper waits.
        assert res.defer_seconds and res.defer_seconds >= 300

    def test_a_queued_body_on_a_200_is_still_submitted_parked(self, monkeypatch):
        install(monkeypatch, json_route(
            200, {"queued": True, "code": "queued_for_next_day"}))
        assert run(status200.PROVIDER.submit(
            API_KEY, payload())).status == "submitted"

    def test_the_park_window_follows_the_providers_own_clock(self, monkeypatch):
        when = datetime.now(timezone.utc) + timedelta(hours=3)
        install(monkeypatch, json_route(202, {
            "queued": True, "scheduled_at": when.isoformat()}))
        res = run(status200.PROVIDER.submit(API_KEY, payload()))
        assert 3 * 3600 - 120 < res.defer_seconds <= 3 * 3600

    def test_quota_headers_are_read(self, monkeypatch):
        install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"},
            headers={"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "3",
                     "X-RateLimit-Reset": "3600"}))
        res = run(status200.PROVIDER.submit(API_KEY, payload()))
        assert res.quota["limit"] == 5 and res.quota["remaining"] == 3
        # A small reset value is a delta, not an epoch in 1970.
        assert res.quota["reset_at"] > datetime.now(timezone.utc)

    def test_an_epoch_reset_is_read_as_an_epoch(self, monkeypatch):
        epoch = int((datetime.now(timezone.utc)
                     + timedelta(hours=2)).timestamp())
        install(monkeypatch, json_route(
            200, {"status": "published", "post_id": "p_1"},
            headers={"X-RateLimit-Reset": str(epoch)}))
        res = run(status200.PROVIDER.submit(API_KEY, payload()))
        assert abs(res.quota["reset_at"].timestamp() - epoch) < 2


# --------------------------------------------------------------------------
# Submit — failure classification
# --------------------------------------------------------------------------
class TestSubmitClassification:
    def test_429_spacing_cooldown_is_rate_limited(self, monkeypatch):
        # NOT quota_exhausted: conflating them parks the post until midnight
        # over a few seconds of throttling.
        install(monkeypatch, json_route(
            429, {"message": "Too many requests, please slow down"},
            headers={"Retry-After": "45"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_RATE_LIMITED
        assert exc.value.defer_seconds == 45
        assert exc.value.retryable and exc.value.is_capacity

    def test_429_naming_the_daily_cap_is_quota_exhausted(self, monkeypatch):
        install(monkeypatch, json_route(
            429, {"code": "daily_limit_reached",
                  "message": "daily limit reached for this account"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_QUOTA_EXHAUSTED
        # Never retried in a few minutes — the cap resets on the provider's day.
        assert exc.value.defer_seconds >= 900

    def test_429_with_an_unparsable_retry_after(self, monkeypatch):
        install(monkeypatch, json_route(
            429, {"message": "slow down"}, headers={"Retry-After": "Wed, 21 Oct"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.defer_seconds == 300

    @pytest.mark.parametrize("status,code,retryable", [
        (401, errors.E_AUTH, False),
        (403, errors.E_NOT_CONNECTED, False),
        (400, errors.E_VALIDATION, False),
        (404, errors.E_VALIDATION, False),
        (413, errors.E_MEDIA_TOO_LARGE, False),
        (422, errors.E_MEDIA_UNFETCHABLE, False),
        (500, errors.E_PROVIDER_5XX, True),
        (503, errors.E_PROVIDER_5XX, True),
    ])
    def test_status_mapping(self, monkeypatch, status, code, retryable):
        install(monkeypatch, json_route(status, {"message": "nope"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == code
        assert exc.value.retryable is retryable

    def test_403_marks_the_destination_not_the_post(self, monkeypatch):
        install(monkeypatch, json_route(403, {"message": "not connected"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code in errors.DESTINATION_FATAL

    def test_401_marks_the_credential(self, monkeypatch):
        install(monkeypatch, json_route(401, {"message": "invalid key"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code in errors.CREDENTIAL_FATAL

    def test_a_submit_timeout_is_ambiguous_and_never_auto_retried(
            self, monkeypatch):
        # THE dangerous case: the provider may have accepted and published. A
        # blind retry double-posts to a real audience.
        def handler(request):
            raise httpx.ReadTimeout("read timed out", request=request)

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_UNKNOWN
        assert exc.value.is_ambiguous is True
        assert exc.value.retryable is False

    def test_a_transport_error_before_a_response_is_retryable(self, monkeypatch):
        # Distinct from the timeout above: nothing reached the provider.
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_NETWORK
        assert exc.value.retryable is True

    def test_a_gateway_5xx_with_an_html_body_is_ambiguous_on_submit(
            self, monkeypatch):
        # The live bug: a 504 from a proxy whose body is an HTML "Inactivity
        # Timeout" page, NOT the provider's JSON. The request POST /api/v2/posts
        # is non-idempotent and its bytes had already been sent and actioned when
        # the proxy gave up on the response — so the post may already be live.
        # Treating that as retryable provider_error is what published a duplicate.
        install(monkeypatch, raw_route(504,
            b"<html><title>Inactivity Timeout</title>"
            b"Too much time has passed without sending any data.</html>"))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_UNKNOWN
        assert exc.value.is_ambiguous is True
        assert exc.value.retryable is False
        assert exc.value.status_code == 504

    def test_a_json_5xx_stays_a_retryable_provider_error_on_submit(
            self, monkeypatch):
        # The other half of the split: a structured JSON 5xx came from the
        # provider's own handler taking its error path deliberately, which IS
        # safely retryable. This is what test_status_mapping asserts too, but
        # here the body is shaped like a real provider error rather than minimal.
        install(monkeypatch, json_route(502, {
            "code": "upstream_failed", "message": "transient backend fault"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_PROVIDER_5XX
        assert exc.value.retryable is True

    def test_a_pool_timeout_before_a_request_is_sent_is_retryable(self, monkeypatch):
        # PoolTimeout means we never got a connection from our own pool, so the
        # request provably never left — retryable, unlike a ReadTimeout.
        def handler(request):
            raise httpx.PoolTimeout("no connection available", request=request)

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_NETWORK
        assert exc.value.retryable is True

    def test_a_read_error_after_the_request_was_sent_is_ambiguous(
            self, monkeypatch):
        # httpx.ReadError happens after bytes were written, so it is NOT in
        # _NEVER_SENT: the post may already have been created. It must be
        # ambiguous, like the ReadTimeout twin.
        def handler(request):
            raise httpx.ReadError("connection reset mid-response", request=request)

        install(monkeypatch, handler)
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_UNKNOWN
        assert exc.value.is_ambiguous is True

    def test_the_error_message_prefers_the_body(self, monkeypatch):
        install(monkeypatch, json_route(400, {"error": {"message": "bad caption"}}))
        with pytest.raises(errors.ProviderError) as exc:
            run(status200.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.message == "bad caption"


# --------------------------------------------------------------------------
# The rest of the contract
# --------------------------------------------------------------------------
class TestCapabilitiesAndLookups:
    def test_probe_verified_capabilities_are_declared_false(self):
        caps = status200.PROVIDER.capabilities
        # Every documented lookup/cancel/list route answered 405 on probe. These
        # flags are what stop the orchestrator building a polling loop.
        assert caps.supports_status_lookup is False
        assert caps.supports_cancel_scheduled is False
        assert caps.supports_account_listing is False
        assert caps.supports_remote_schedule is False
        assert caps.supports_webhooks is True
        assert caps.one_platform_per_request is True
        assert caps.media_by_url is True
        assert caps.media_ref_ttl_seconds == 7 * 24 * 3600

    def test_fetch_status_reports_unsupported_rather_than_raising(self):
        # None lets the reconciler treat "no polling" as normal and fall through
        # to the stale sweeper.
        assert run(status200.PROVIDER.fetch_status(API_KEY, "p_1")) is None

    def test_verify_destination_never_posts(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, {}))
        out = run(status200.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_123"))
        assert out["health"] == "unverified"
        assert out["detail"]
        assert seen == []


class TestCheckCredential:
    def test_401_is_a_bad_key(self, monkeypatch):
        install(monkeypatch, json_route(401, {"message": "unauthorized"}))
        out = run(status200.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False and out["code"] == errors.E_AUTH

    @pytest.mark.parametrize("status", [200, 400, 422])
    def test_anything_else_proves_the_key_authenticated(self, monkeypatch, status):
        # Auth is evaluated before request validation, so a 4xx about the
        # deliberately invalid URL still means the key was accepted.
        install(monkeypatch, json_route(status, {"message": "bad url"}))
        assert run(status200.PROVIDER.check_credential(API_KEY))["ok"] is True

    def test_403_is_reported_as_not_authorized(self, monkeypatch):
        install(monkeypatch, json_route(403, {}))
        out = run(status200.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False and out["code"] == errors.E_NOT_CONNECTED

    def test_a_provider_outage_is_not_a_bad_key(self, monkeypatch):
        install(monkeypatch, json_route(503, {}))
        out = run(status200.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False and out["code"] == errors.E_PROVIDER_5XX

    def test_an_unreachable_provider_is_reported_not_raised(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("dns failure", request=request)

        install(monkeypatch, handler)
        out = run(status200.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False and out["code"] == errors.E_NETWORK

    def test_the_probe_url_is_deliberately_unresolvable(self, monkeypatch):
        # It must not be a real URL: a credential check may not create anything.
        seen = install(monkeypatch, json_route(400, {}))
        run(status200.PROVIDER.check_credential(API_KEY))
        assert body_of(seen[0])["url"].endswith(".invalid/credential-check")


class TestWebhookParsing:
    def test_published_envelope(self):
        ev = status200.PROVIDER.parse_webhook({
            "id": "evt_1", "type": "post.published",
            "created_at": "2026-08-09T12:00:00Z",
            "data": {"post_id": "p_1", "platform_post_id": "IG_9",
                     "accountId": "acct_123",
                     "permalink": "https://ig.test/p/9"}})
        assert ev.event_id == "evt_1"
        assert ev.event_type == "post.published"
        assert ev.provider_post_ref == "p_1"
        assert ev.provider_native_post_ref == "IG_9"
        assert ev.provider_account_ref == "acct_123"
        assert ev.permalink == "https://ig.test/p/9"
        assert ev.created_at == pytest.approx(
            datetime(2026, 8, 9, 12, tzinfo=timezone.utc).timestamp())

    def test_failed_envelope_carries_the_reason(self):
        ev = status200.PROVIDER.parse_webhook({
            "id": "evt_2", "type": "post.failed",
            "data": {"post_id": "p_2", "error": "token expired"}})
        assert ev.event_type == "post.failed"
        assert ev.error_message == "token expired"
        # A definite refusal has no opinion to offer — the dispatcher treats it
        # as a normal retryable provider failure.
        assert ev.error_code is None

    def test_a_failed_timeout_is_classified_ambiguous(self):
        # The live bug: a clip that reached TikTok came back "Timeout" because
        # Status 200 never received the platform's confirmation. That tells us
        # nothing about whether the post is live, so it must be E_UNKNOWN —
        # auto-retrying it would double-publish to a real audience.
        for wording in ("Timeout", "No confirmation in time",
                        "timed out waiting for the platform",
                        "unknown publish result"):
            ev = status200.PROVIDER.parse_webhook({
                "id": "evt_t", "type": "post.failed",
                "data": {"post_id": "p_t", "error": wording}})
            assert ev.event_type == "post.failed"
            assert ev.error_code == errors.E_UNKNOWN, wording

    def test_a_published_event_has_no_error_code(self):
        ev = status200.PROVIDER.parse_webhook({
            "id": "evt_ok", "type": "post.published",
            "data": {"post_id": "p_ok", "error": "Timeout"}})
        assert ev.error_code is None

    def test_a_malformed_failure_with_no_ambiguous_word_is_a_plain_failure(self):
        ev = status200.PROVIDER.parse_webhook({
            "id": "evt_x", "type": "post.failed",
            "data": {"post_id": "p_x", "error": "video aspect ratio rejected"}})
        assert ev.error_code is None

    def test_profile_disconnected_maps_to_the_neutral_name(self):
        # The provider's vocabulary ("profile") must not leak past the adapter.
        ev = status200.PROVIDER.parse_webhook({
            "id": "evt_3", "type": "profile.disconnected",
            "data": {"profile": "acct_123"}})
        assert ev.event_type == "account.disconnected"
        assert ev.provider_account_ref == "acct_123"

    def test_an_unrecognized_type_is_unknown_not_an_exception(self):
        # A new provider event type must not 500 the webhook route — the ack has
        # to go out within ~5s or the provider retries.
        ev = status200.PROVIDER.parse_webhook({"id": "evt_4", "type": "post.eaten"})
        assert ev.event_type == "unknown"

    def test_a_malformed_body_still_parses(self):
        ev = status200.PROVIDER.parse_webhook({"data": "not-a-dict"})
        assert ev.event_id == "" and ev.event_type == "unknown"
        assert ev.provider_post_ref is None


# --------------------------------------------------------------------------
# Registry + the fake provider
# --------------------------------------------------------------------------
class TestRegistry:
    def test_dry_run_resolves_every_name_to_the_fake(self, monkeypatch):
        # This is what makes the whole pipeline runnable before a credential
        # exists — and what makes an accidental live post impossible while it is
        # on.
        monkeypatch.setenv("PUBLISHING_DRY_RUN", "true")
        assert providers.get("status200") is fake.PROVIDER
        assert providers.get("anything-at-all") is fake.PROVIDER

    def test_live_mode_resolves_the_real_adapter(self, monkeypatch):
        monkeypatch.setenv("PUBLISHING_DRY_RUN", "false")
        assert providers.get("status200") is status200.PROVIDER

    def test_an_unknown_provider_names_the_registered_ones(self, monkeypatch):
        monkeypatch.setenv("PUBLISHING_DRY_RUN", "false")
        with pytest.raises(KeyError) as exc:
            providers.get("hootsuite")
        assert "status200" in str(exc.value)

    def test_both_adapters_are_registered(self):
        assert set(providers.names()) >= {"status200", "fake"}

    def test_describe_is_ui_safe(self):
        rows = {r["name"]: r for r in providers.describe()}
        assert rows["status200"]["supports_account_listing"] is False
        assert rows["status200"]["platforms"] == ["youtube", "instagram", "tiktok"]
        # No credential, key, or secret field anywhere in the UI payload.
        assert not any(k in json.dumps(rows).lower()
                       for k in ("api_key", "secret", "token"))


class TestFakeProvider:
    def setup_method(self):
        fake.reset()

    def test_it_mirrors_status200s_capabilities(self):
        real, mock = status200.PROVIDER.capabilities, fake.PROVIDER.capabilities
        for flag in ("supports_media_refs", "media_by_url",
                     "supports_status_lookup", "supports_remote_schedule",
                     "supports_cancel_scheduled", "supports_account_listing",
                     "supports_webhooks", "one_platform_per_request"):
            assert getattr(mock, flag) == getattr(real, flag), flag
        # Otherwise dry-run would exercise branches production never takes.
        assert mock.platforms == real.platforms

    def test_the_happy_path_publishes(self):
        res = run(fake.PROVIDER.submit(API_KEY, payload()))
        assert res.status == "succeeded"
        assert res.provider_post_ref and res.permalink
        assert len(fake.submissions) == 1

    @pytest.mark.parametrize("marker,code,ambiguous", [
        ("fail-auth", errors.E_AUTH, False),
        ("fail-blocked", errors.E_NOT_CONNECTED, False),
        ("fail-transient", errors.E_NETWORK, False),
        ("fail-oversize", errors.E_MEDIA_TOO_LARGE, False),
        ("fail-unknown", errors.E_UNKNOWN, True),
        ("fail-ambiguous", errors.E_UNKNOWN, True),
    ])
    def test_markers_force_each_failure_mode(self, marker, code, ambiguous):
        with pytest.raises(errors.ProviderError) as exc:
            run(fake.PROVIDER.submit(API_KEY, payload(caption=f"clip {marker}")))
        assert exc.value.code == code
        assert exc.value.is_ambiguous is ambiguous
        # Recorded even though it failed — the attempt happened.
        assert fake.submissions[-1]["marker"] == marker

    def test_the_quota_marker_parks_the_post_as_submitted(self):
        res = run(fake.PROVIDER.submit(API_KEY, payload(caption="quota test")))
        # A 202 parks the post on the provider side — it exists there, so it is
        # NOT a deferral-to-resent. status="submitted" with a window keeps it live.
        assert res.status == "submitted"
        assert res.provider_post_ref
        assert res.quota["remaining"] == 0
        assert res.defer_seconds
        assert fake.submissions[-1]["marker"] == "quota"

    def test_the_refuse_capacity_marker_is_a_true_deferral(self):
        # The other capacity shape: the provider created nothing and said "later",
        # so the same payload must be submitted again once the cooldown passes.
        res = run(fake.PROVIDER.submit(API_KEY,
                                       payload(caption="refuse-capacity test")))
        assert res.status == "deferred"
        assert res.provider_post_ref is None
        assert res.defer_seconds
        assert fake.submissions[-1]["marker"] == "refuse-capacity"

    def test_the_slow_marker_awaits_a_webhook(self):
        res = run(fake.PROVIDER.submit(API_KEY, payload(caption="slow clip")))
        assert res.status == "submitted"
        assert res.provider_post_ref

    def test_a_marker_in_the_account_ref_works_too(self):
        with pytest.raises(errors.ProviderError):
            run(fake.PROVIDER.submit(
                API_KEY, payload(provider_account_ref="acct-fail-blocked")))

    def test_no_key_is_rejected_like_a_real_provider(self):
        with pytest.raises(errors.ProviderError) as exc:
            run(fake.PROVIDER.submit("", payload()))
        assert exc.value.code == errors.E_AUTH

    def test_media_refs_are_content_addressed(self):
        url = "https://cdn.example.test/clip.mp4"
        a = run(fake.PROVIDER.upload_media(API_KEY, media_url=url))
        b = run(fake.PROVIDER.upload_media(API_KEY, media_url=url))
        # Same bytes -> same ref, so the reuse cache is testable in dry run.
        assert a.ref == b.ref
        c = run(fake.PROVIDER.upload_media(
            API_KEY, media_url="https://cdn.example.test/other.mp4"))
        assert c.ref != a.ref
        assert len(fake.uploads) == 3

    def test_fetch_status_is_unsupported_here_too(self):
        assert run(fake.PROVIDER.fetch_status(API_KEY, "p_1")) is None

    def test_reset_clears_the_ledger(self):
        run(fake.PROVIDER.submit(API_KEY, payload()))
        fake.reset()
        assert fake.submissions == [] and fake.uploads == []
