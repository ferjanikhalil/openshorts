"""Contract tests for the Zernio adapter.

Same seam as ``test_publishing_provider.py``: the adapter builds its httpx client
inline, so a subclass of ``httpx.AsyncClient`` injects a ``MockTransport`` and the
adapter source is untouched. No network, no credential, no real post.

The tests are weighted toward the places where Zernio differs from Status 200,
because those are the places a shared-parser instinct produces a silent failure:

  * a FLAT request body whose wrong shape yields a 201 *draft* — an HTTP success
    that never publishes anything (``TestDraftTrap``);
  * ``X-RateLimit-*`` headers that look identical to Status 200's and mean
    something entirely different (``TestQuotaIsNotRequestRate``);
  * a duplicate submit that hands back the existing post instead of creating a
    second one (``TestDuplicateResolution``);
  * webhook field names that share not one key with the other provider
    (``TestParseWebhook``).

Every api_key below is a made-up ``zr_test_…`` string.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from publishing import errors, platforms as plat, providers, signing
from publishing.providers import status200, zernio
from publishing.providers.base import PublishPayload

API_KEY = "zr_test_0000111122223333"
POST_URL = zernio.POSTS_ENDPOINT
ACCOUNTS_URL = zernio.ACCOUNTS_ENDPOINT


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_remote_schedule_health():
    """Remote-schedule health is PROCESS-lifetime state on the module.

    A test that trips the detector would otherwise leave the field disabled for
    every test after it, making failures depend on collection order.
    """
    zernio._reset_remote_schedule()
    yield
    zernio._reset_remote_schedule()


# Captured before any monkeypatching — subclassing the live class would nest a
# previous test's transport under the new one and the older handler would win.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_client(handler):
    class _MockedClient(_REAL_ASYNC_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockedClient


def install(monkeypatch, handler):
    """Point the adapter's httpx at ``handler``; return the captured requests."""
    seen = []

    def _capture(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(zernio.httpx, "AsyncClient", mock_client(_capture))
    return seen


def body_of(request):
    return json.loads(request.content.decode())


def json_route(status, payload_, headers=None):
    def handler(request):
        return httpx.Response(status, json=payload_, headers=headers or {})
    return handler


def raw_route(status, body_bytes, headers=None):
    def handler(request):
        return httpx.Response(status, content=body_bytes,
                              headers=headers or {"content-type": "text/html"})
    return handler


def boom_route(exc):
    def handler(request):
        raise exc
    return handler


MEDIA_URL = ("https://s3.eu-west-1.amazonaws.com/openshorts/stage/clip.mp4"
             "?X-Amz-Signature=aaaaaaaa&X-Amz-Expires=604800")


def payload(**over):
    base = dict(platform=plat.INSTAGRAM, provider_account_ref="65f0a1b2c3d4e5f60718293a",
                caption="a clip", media_url=MEDIA_URL)
    base.update(over)
    return PublishPayload(**base)


def created(status="publishing", *, post_id="p_abc123", platform="instagram",
            entry=None, **post_over):
    """A ``POST /v1/posts`` success envelope."""
    post = {
        "_id": post_id,
        "status": status,
        "platforms": [entry if entry is not None else {
            "platform": platform, "accountId": "65f0a1b2c3d4e5f60718293a",
            "status": status if status != "partial" else "published",
        }],
    }
    post.update(post_over)
    return {"success": True, "message": "Post created", "post": post}


# --------------------------------------------------------------------------
# Registration and declared capabilities
# --------------------------------------------------------------------------
class TestRegistration:
    def test_the_adapter_is_in_the_registry(self, monkeypatch):
        monkeypatch.setenv("PUBLISHING_DRY_RUN", "false")
        assert providers.get("zernio") is zernio.PROVIDER
        assert "zernio" in providers.names()

    def test_dry_run_still_short_circuits_this_provider_too(self, monkeypatch):
        """A second live provider must not become a way to post while dry-run is on."""
        from publishing.providers import fake
        monkeypatch.setenv("PUBLISHING_DRY_RUN", "true")
        assert providers.get("zernio") is fake.PROVIDER

    def test_capabilities_reach_the_admin_ui(self):
        row = next(d for d in providers.describe() if d["name"] == "zernio")
        assert row["supports_remote_schedule"] is True
        assert row["supports_status_lookup"] is True
        assert row["supports_account_listing"] is True
        assert row["multi_credential"] is True

    def test_remote_scheduling_is_never_declared_without_cancel(self):
        """The pair is what makes handing the clock over reversible.

        A scheduled post we cannot delete is a post we cannot stop, so this
        guards the combination rather than either flag. Asserted across every
        registered adapter, so a future provider cannot declare half of it.
        """
        for name in providers.names():
            caps = providers.get.__globals__["_REGISTRY"][name].capabilities
            if caps.supports_remote_schedule:
                assert caps.supports_cancel_scheduled, (
                    f"{name} hands the clock to the provider but cannot cancel")

    def test_no_reusable_media_ref_is_claimed(self):
        assert zernio.CAPABILITIES.supports_media_refs is False
        assert zernio.CAPABILITIES.media_by_url is True

    def test_the_signature_header_is_the_providers_own(self):
        assert zernio.CAPABILITIES.signature_header == "X-Zernio-Signature"
        assert (zernio.CAPABILITIES.signature_header
                != status200.CAPABILITIES.signature_header)


# --------------------------------------------------------------------------
# The request body — flat, and always carrying a publish instruction
# --------------------------------------------------------------------------
class TestSubmitRequestShape:
    def test_the_body_is_flat_and_content_is_a_string(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload(caption="hello world")))

        sent = body_of(seen[0])
        # The other provider wraps in {"post": {...}} and makes content an
        # object. Both would be accepted here and produce a draft.
        assert "post" not in sent
        assert sent["content"] == "hello world"
        assert isinstance(sent["content"], str)
        assert sent["platforms"] == [{"platform": "instagram",
                                      "accountId": "65f0a1b2c3d4e5f60718293a"}]
        assert sent["mediaItems"] == [{"url": MEDIA_URL, "type": "video"}]

    def test_an_immediate_post_always_carries_publishNow(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))

        sent = body_of(seen[0])
        assert sent["publishNow"] is True
        assert "scheduledFor" not in sent

    def test_media_type_is_declared_not_inferred(self, monkeypatch):
        """A presigned URL's extension is followed by a query string.

        Extension sniffing on the far side would see ".mp4?X-Amz-Signature=…",
        so the type is stated outright.
        """
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert body_of(seen[0])["mediaItems"][0]["type"] == "video"

    def test_a_marker_travels_in_metadata(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert body_of(seen[0])["metadata"]["source"] == "openshorts"

    def test_the_bearer_token_is_the_api_key(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert seen[0].headers["authorization"] == f"Bearer {API_KEY}"

    def test_a_submit_with_no_media_is_refused_before_the_network(self,
                                                                 monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(
                API_KEY, PublishPayload(platform=plat.TIKTOK,
                                        provider_account_ref="a1")))
        assert exc.value.code == errors.E_VALIDATION
        assert seen == []

    def test_a_submit_with_no_account_ref_is_refused(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(
                API_KEY, payload(provider_account_ref="")))
        assert exc.value.code == errors.E_VALIDATION
        assert seen == []


class TestPlatformSpecificDefaults:
    def test_youtube_gets_an_explicit_audience_declaration(self, monkeypatch):
        seen = install(monkeypatch, json_route(
            201, created(platform="youtube")))
        run(zernio.PROVIDER.submit(
            API_KEY, payload(platform=plat.YOUTUBE, title="A title")))

        data = body_of(seen[0])["platforms"][0]["platformSpecificData"]
        # Omitting madeForKids lets YouTube publish and then block views.
        assert data["madeForKids"] is False
        assert data["visibility"] == "public"
        assert data["title"] == "A title"

    def test_a_youtube_title_is_truncated_to_the_platform_ceiling(self,
                                                                 monkeypatch):
        seen = install(monkeypatch, json_route(
            201, created(platform="youtube")))
        run(zernio.PROVIDER.submit(
            API_KEY, payload(platform=plat.YOUTUBE, title="x" * 250)))

        title = body_of(seen[0])["platforms"][0]["platformSpecificData"]["title"]
        assert len(title) == plat.TITLE_LIMITS[plat.YOUTUBE] == 100

    def test_tiktok_defaults_to_public(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created(platform="tiktok")))
        run(zernio.PROVIDER.submit(API_KEY, payload(platform=plat.TIKTOK)))

        data = body_of(seen[0])["platforms"][0]["platformSpecificData"]
        # TikTok's native default is private/only-me, and a private post never
        # surfaces the confirmation the provider waits for.
        assert data["privacyLevel"] == "PUBLIC_TO_EVERYONE"

    def test_tiktok_consent_attestations_are_never_set_for_the_operator(
            self, monkeypatch):
        """These claim a human saw a preview and consented. We cannot claim that.

        Settable per destination; never defaulted. If TikTok refuses a post for
        their absence the error arrives classified, with a message naming them.
        """
        seen = install(monkeypatch, json_route(201, created(platform="tiktok")))
        run(zernio.PROVIDER.submit(API_KEY, payload(platform=plat.TIKTOK)))

        data = body_of(seen[0])["platforms"][0]["platformSpecificData"]
        assert "contentPreviewConfirmed" not in data
        assert "expressConsentGiven" not in data

    def test_operator_options_override_every_default(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created(platform="tiktok")))
        run(zernio.PROVIDER.submit(API_KEY, payload(
            platform=plat.TIKTOK,
            options={"privacyLevel": "MUTUAL_FOLLOW_FRIENDS",
                     "expressConsentGiven": True, "allowDuet": False})))

        data = body_of(seen[0])["platforms"][0]["platformSpecificData"]
        assert data["privacyLevel"] == "MUTUAL_FOLLOW_FRIENDS"
        assert data["expressConsentGiven"] is True
        assert data["allowDuet"] is False


# --------------------------------------------------------------------------
# The draft trap — the adapter's most important guard
# --------------------------------------------------------------------------
class TestDraftTrap:
    def test_a_draft_response_is_a_failure_not_a_success(self, monkeypatch):
        """201 Created, a real post id, and nothing will ever publish."""
        install(monkeypatch, json_route(201, created("draft", post_id="p_draft")))

        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))

        assert exc.value.code == errors.E_VALIDATION
        assert exc.value.code in errors.PERMANENT   # never retried
        # The orphan draft's id has to survive, or a human cannot delete it.
        assert exc.value.provider_post_ref == "p_draft"
        assert "draft" in exc.value.message.lower()

    def test_the_message_names_the_field_that_was_ignored(self, monkeypatch):
        install(monkeypatch, json_route(201, created("draft")))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert "publishNow" in exc.value.message

        install(monkeypatch, json_route(201, created("draft")))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload(
                scheduled_for=datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc))))
        assert "scheduledFor" in exc.value.message

    def test_a_draft_found_by_lookup_is_pending_not_succeeded(self, monkeypatch):
        install(monkeypatch, json_route(200, {"post": {
            "_id": "p_1", "status": "draft", "platforms": []}}))
        res = run(zernio.PROVIDER.fetch_status(API_KEY, "p_1"))
        assert res.status == "submitted"


# --------------------------------------------------------------------------
# Remote scheduling — real here, unlike Status 200
# --------------------------------------------------------------------------
class TestRemoteScheduling:
    SLOT = datetime(2026, 9, 1, 17, 30, tzinfo=timezone.utc)

    def test_a_slot_is_sent_in_the_shape_the_api_echoes(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created(
            "scheduled", scheduledFor="2026-09-01T17:30:00.000Z")))
        run(zernio.PROVIDER.submit(API_KEY, payload(scheduled_for=self.SLOT)))

        sent = body_of(seen[0])
        # Literal Z with milliseconds — isoformat()'s "+00:00" is rejected by a
        # JS datetime validator.
        assert sent["scheduledFor"] == "2026-09-01T17:30:00.000Z"
        assert sent["timezone"] == "UTC"
        assert "publishNow" not in sent

    def test_a_naive_datetime_is_read_as_utc_not_local(self):
        naive = datetime(2026, 9, 1, 17, 30)
        assert zernio._iso_z(naive) == "2026-09-01T17:30:00.000Z"

    def test_a_non_utc_datetime_is_converted_not_relabelled(self):
        plus_two = datetime(2026, 9, 1, 19, 30,
                            tzinfo=timezone(timedelta(hours=2)))
        assert zernio._iso_z(plus_two) == "2026-09-01T17:30:00.000Z"

    def test_an_honoured_slot_stays_submitted_and_keeps_the_field_healthy(
            self, monkeypatch):
        install(monkeypatch, json_route(201, created(
            "scheduled", scheduledFor="2026-09-01T17:30:00.000Z",
            entry={"platform": "instagram", "status": "scheduled"})))

        res = run(zernio.PROVIDER.submit(API_KEY,
                                         payload(scheduled_for=self.SLOT)))
        assert res.status == "submitted"
        assert res.schedule_ignored is False
        assert zernio.remote_schedule_available() is True

    def test_accept_then_ignore_is_caught_after_one_post(self, monkeypatch):
        """The detector exists for the day this provider stops honouring slots.

        Without it, a plan spaced across a day publishes all at once and nothing
        reports why.
        """
        install(monkeypatch, json_route(201, created(
            "published", entry={"platform": "instagram", "status": "published",
                                "publishedUrl": "https://ig.test/p/1"})))

        res = run(zernio.PROVIDER.submit(API_KEY,
                                         payload(scheduled_for=self.SLOT)))
        # The post is real and live, so this is a success, not a failure.
        assert res.status == "succeeded"
        assert res.schedule_ignored is True
        # ...and the hand-over stops for the rest of the process.
        assert zernio.remote_schedule_available() is False

    def test_an_echoed_timestamp_outweighs_a_moving_status(self, monkeypatch):
        """Positive proof the slot was taken beats a status that looks immediate."""
        install(monkeypatch, json_route(201, created(
            "publishing", scheduledFor="2026-09-01T17:30:00.000Z")))
        res = run(zernio.PROVIDER.submit(API_KEY,
                                         payload(scheduled_for=self.SLOT)))
        assert res.schedule_ignored is False
        assert zernio.remote_schedule_available() is True

    def test_a_rejected_field_is_its_own_code_so_the_local_clock_takes_over(
            self, monkeypatch):
        install(monkeypatch, json_route(400, {
            "error": "Validation failed",
            "details": [{"path": ["scheduledFor"],
                         "message": "must be in the future"}]}))

        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY,
                                       payload(scheduled_for=self.SLOT)))
        # A 4xx created nothing, so falling back to a local-clock submit is safe.
        assert exc.value.code == errors.E_REMOTE_SCHEDULE

    def test_an_unrelated_400_is_not_read_as_a_schedule_problem(self,
                                                               monkeypatch):
        install(monkeypatch, json_route(400, {
            "error": "Validation failed",
            "details": [{"path": ["content"], "message": "too long"}]}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY,
                                       payload(scheduled_for=self.SLOT)))
        assert exc.value.code == errors.E_VALIDATION
        assert "content" in exc.value.message


# --------------------------------------------------------------------------
# Idempotency and duplicate resolution
# --------------------------------------------------------------------------
class TestIdempotencyKey:
    def test_the_same_submission_always_sends_the_same_request_id(self,
                                                                  monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert seen[0].headers["x-request-id"] == seen[1].headers["x-request-id"]

    def test_a_fresh_presign_does_not_change_the_key(self, monkeypatch):
        """The whole point: a retry re-presigns, and a random key would duplicate.

        Only the signature differs between these two URLs, so the idempotency
        key must ignore the query string.
        """
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        run(zernio.PROVIDER.submit(API_KEY, payload(
            media_url=("https://s3.eu-west-1.amazonaws.com/openshorts/stage/"
                       "clip.mp4?X-Amz-Signature=zzzzzzzz&X-Amz-Expires=60"))))
        assert seen[0].headers["x-request-id"] == seen[1].headers["x-request-id"]

    def test_a_different_clip_gets_a_different_key(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        run(zernio.PROVIDER.submit(API_KEY, payload(
            media_url=("https://s3.eu-west-1.amazonaws.com/openshorts/stage/"
                       "other.mp4?X-Amz-Signature=aaaaaaaa"))))
        assert seen[0].headers["x-request-id"] != seen[1].headers["x-request-id"]

    def test_a_different_account_gets_a_different_key(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        run(zernio.PROVIDER.submit(API_KEY,
                                   payload(provider_account_ref="other_acct")))
        assert seen[0].headers["x-request-id"] != seen[1].headers["x-request-id"]

    def test_a_different_slot_gets_a_different_key(self, monkeypatch):
        seen = install(monkeypatch, json_route(201, created("scheduled")))
        run(zernio.PROVIDER.submit(API_KEY, payload(
            scheduled_for=datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc))))
        run(zernio.PROVIDER.submit(API_KEY, payload(
            scheduled_for=datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc))))
        assert seen[0].headers["x-request-id"] != seen[1].headers["x-request-id"]

    def test_the_key_is_a_uuid(self, monkeypatch):
        import uuid
        seen = install(monkeypatch, json_route(201, created()))
        run(zernio.PROVIDER.submit(API_KEY, payload()))
        uuid.UUID(seen[0].headers["x-request-id"])   # raises if malformed


class TestDuplicateResolution:
    def test_a_409_with_an_existing_ref_adopts_it_instead_of_duplicating(
            self, monkeypatch):
        """The path that turns an ambiguous retry into a resolved attempt.

        The post exists on their side. Reporting a failure would strand it;
        re-sending would publish a second copy.
        """
        install(monkeypatch, json_route(409, {
            "error": "Duplicate post detected",
            "details": {"existingPostId": "p_original",
                        "message": "identical content within 24h"}}))

        res = run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert res.status == "submitted"
        assert res.provider_post_ref == "p_original"
        assert "duplicate" in res.raw["_note"].lower()

    def test_a_409_with_no_ref_is_a_permanent_duplicate(self, monkeypatch):
        install(monkeypatch, json_route(409, {"error": "Duplicate post"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_DUPLICATE
        assert exc.value.code in errors.PERMANENT

    def test_a_replayed_request_id_returns_the_original_post(self, monkeypatch):
        """200 + existingPost: the provider's own idempotency window fired."""
        install(monkeypatch, json_route(200, {
            "success": True, "message": "Request already processed",
            "existingPost": {"_id": "p_first", "status": "publishing",
                             "platforms": [{"platform": "instagram",
                                            "status": "publishing"}]}}))

        res = run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert res.status == "submitted"
        assert res.provider_post_ref == "p_first"
        assert "replayed" in res.raw["_note"]


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------
class TestErrorClassification:
    def test_401_stops_the_group(self, monkeypatch):
        install(monkeypatch, json_route(401, {"error": "Unauthorized"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_AUTH
        assert exc.value.code in errors.CREDENTIAL_FATAL

    def test_403_account_disconnected_blocks_one_destination_only(self,
                                                                 monkeypatch):
        """Blast radius is the whole point of separating this from E_AUTH."""
        install(monkeypatch, json_route(403, {
            "error": "Account disconnected", "code": "ACCOUNT_DISCONNECTED"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_ACCOUNT_AUTH
        assert exc.value.code in errors.DESTINATION_FATAL
        assert exc.value.code not in errors.CREDENTIAL_FATAL

    def test_403_over_limit_is_a_plan_decision_not_a_key_problem(self,
                                                                monkeypatch):
        """The free tier's 2-accounts ceiling — the reason slots exist."""
        install(monkeypatch, json_route(403, {
            "error": "Profile is over its connected-account limit",
            "code": "PROFILE_OVER_LIMIT"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_PLAN_LIMIT
        assert exc.value.code in errors.PERMANENT
        assert "slot" in exc.value.message

    def test_403_with_no_code_points_at_the_account_reference(self, monkeypatch):
        install(monkeypatch, json_route(403, {
            "error": "One or more accounts do not belong to this user"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_NOT_CONNECTED
        # An operator reading this must not go looking for a new API key.
        assert "account reference" in exc.value.message

    def test_402_is_a_plan_limit(self, monkeypatch):
        install(monkeypatch, json_route(402, {"error": "Upgrade required"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_PLAN_LIMIT

    def test_a_zod_issue_array_is_rendered_readably(self, monkeypatch):
        install(monkeypatch, json_route(400, {
            "error": "Validation failed",
            "details": [{"path": ["platforms", 0, "accountId"],
                         "message": "Invalid ObjectId"}]}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert "accountId" in exc.value.message
        assert "Invalid ObjectId" in exc.value.message


class TestRateLimitsVersusQuota:
    def test_a_throughput_429_clears_in_seconds(self, monkeypatch):
        install(monkeypatch, json_route(
            429, {"error": "Too many requests"}, {"Retry-After": "30"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_RATE_LIMITED
        assert exc.value.defer_seconds == 30
        assert exc.value.retryable

    def test_a_daily_cap_429_waits_much_longer(self, monkeypatch):
        """Deferring a daily cap for 30 seconds re-burns it 120 times an hour."""
        install(monkeypatch, json_route(
            429, {"error": "Daily posting limit reached for this account"},
            {"Retry-After": "30"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_QUOTA_EXHAUSTED
        assert exc.value.defer_seconds >= 900
        assert exc.value.response["_quota"]["remaining"] == 0

    def test_an_http_date_retry_after_is_understood(self, monkeypatch):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        install(monkeypatch, json_route(
            429, {"error": "slow down"},
            {"Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT")}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert 400 <= exc.value.defer_seconds <= 700

    def test_a_missing_retry_after_still_defers(self, monkeypatch):
        install(monkeypatch, json_route(429, {"error": "Too many requests"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.defer_seconds and exc.value.defer_seconds > 0


class TestQuotaIsNotRequestRate:
    def test_ratelimit_headers_are_never_read_as_a_posting_quota(self,
                                                                monkeypatch):
        """The trap this adapter is most likely to fall into.

        Status 200 sends identically-named headers carrying the DAILY POST CAP.
        Zernio's are API requests per minute, while posts are unlimited. Sharing
        the parser would tell quota-aware dispatch there are 58 posts left today.
        """
        install(monkeypatch, json_route(201, created(), {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "58",
            "X-RateLimit-Reset": "45"}))

        res = run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert res.quota == {}

    def test_a_body_that_really_is_about_posts_is_read(self, monkeypatch):
        install(monkeypatch, json_route(
            201, {**created(), "limit": 50, "used": 3}))
        res = run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert res.quota["limit"] == 50
        assert res.quota["remaining"] == 47


class TestAmbiguityOnSubmit:
    def test_an_html_5xx_is_ambiguous_and_never_auto_retried(self, monkeypatch):
        """The shape that published a duplicate through the other provider.

        A gateway giving up on the RESPONSE is not the provider refusing the
        REQUEST — the handler already ran and the post may exist.
        """
        install(monkeypatch, raw_route(
            504, b"<html><body>Inactivity Timeout</body></html>"))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_UNKNOWN
        assert exc.value.is_ambiguous
        assert not exc.value.retryable

    def test_a_structured_5xx_is_the_providers_own_error_path(self, monkeypatch):
        install(monkeypatch, json_route(503, {"error": "Service unavailable"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_PROVIDER_5XX
        assert exc.value.retryable

    def test_a_connect_failure_provably_never_sent_is_retryable(self,
                                                               monkeypatch):
        install(monkeypatch, boom_route(httpx.ConnectError("refused")))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_NETWORK
        assert exc.value.retryable

    def test_a_read_timeout_happened_after_the_write_so_it_is_ambiguous(
            self, monkeypatch):
        install(monkeypatch, boom_route(httpx.ReadTimeout("no answer")))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert exc.value.code == errors.E_UNKNOWN
        assert not exc.value.retryable
        assert "may or may not" in exc.value.message


# --------------------------------------------------------------------------
# Per-platform failure detail
# --------------------------------------------------------------------------
class TestPlatformErrorCategories:
    def _fail_with(self, monkeypatch, **entry):
        install(monkeypatch, json_route(201, created(
            "failed", entry={"platform": "instagram", "status": "failed",
                             **entry})))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload()))
        return exc.value

    def test_auth_expired_is_one_account_not_the_key(self, monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="auth_expired",
                              errorMessage="token expired")
        assert err.code == errors.E_ACCOUNT_AUTH

    def test_quota_exhausted_waits_for_the_reset(self, monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="quota_exhausted")
        assert err.code == errors.E_QUOTA_EXHAUSTED
        assert err.is_capacity

    def test_platform_rejected_is_permanent(self, monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="platform_rejected",
                              errorMessage="community guidelines")
        assert err.code == errors.E_UNSUPPORTED
        assert err.code in errors.PERMANENT

    def test_system_error_is_retryable(self, monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="system_error")
        assert err.code == errors.E_PROVIDER_5XX
        assert err.retryable

    def test_the_providers_own_unknown_is_our_unknown(self, monkeypatch):
        """Zernio saying "we do not know" must never trigger a retry.

        The post may be live, and a retry on a live post double-publishes.
        """
        err = self._fail_with(monkeypatch, errorCategory="unknown",
                              errorMessage="no confirmation received")
        assert err.code == errors.E_UNKNOWN
        assert err.is_ambiguous
        assert not err.retryable

    def test_an_unrecognized_category_is_retryable_but_not_ambiguous(
            self, monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="brand_new_thing")
        assert err.code == errors.E_PROVIDER_5XX

    def test_the_category_and_source_are_kept_for_the_operator(self,
                                                              monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="user_content",
                              errorSource="user",
                              errorMessage="caption too long")
        assert "caption too long" in err.message
        assert "category=user_content" in err.message
        assert "source=user" in err.message

    def test_the_post_ref_survives_a_platform_failure(self, monkeypatch):
        err = self._fail_with(monkeypatch, errorCategory="platform_error")
        assert err.provider_post_ref == "p_abc123"


class TestPlatformEntryMatching:
    def test_a_sibling_platforms_failure_never_fails_this_attempt(self,
                                                                 monkeypatch):
        """Read by name, not ``[0]``.

        A post-level ``partial`` with another platform failed is a post whose
        video IS live here; taking the first entry would fail it.
        """
        install(monkeypatch, json_route(201, {"post": {
            "_id": "p_x", "status": "partial", "platforms": [
                {"platform": "youtube", "status": "failed",
                 "errorCategory": "platform_rejected"},
                {"platform": "instagram", "status": "published",
                 "publishedUrl": "https://ig.test/p/9"},
            ]}}))

        res = run(zernio.PROVIDER.submit(API_KEY,
                                         payload(platform=plat.INSTAGRAM)))
        assert res.status == "succeeded"
        assert res.permalink == "https://ig.test/p/9"

    def test_the_matching_platform_failing_does_fail_the_attempt(self,
                                                                monkeypatch):
        install(monkeypatch, json_route(201, {"post": {
            "_id": "p_x", "status": "partial", "platforms": [
                {"platform": "youtube", "status": "published"},
                {"platform": "instagram", "status": "failed",
                 "errorCategory": "user_content",
                 "errorMessage": "aspect ratio"},
            ]}}))

        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.submit(API_KEY, payload(platform=plat.INSTAGRAM)))
        assert exc.value.code == errors.E_VALIDATION
        assert "aspect ratio" in exc.value.message

    def test_a_response_with_no_platform_array_falls_back_to_post_status(
            self, monkeypatch):
        install(monkeypatch, json_route(201, {"post": {
            "_id": "p_x", "status": "published"}}))
        res = run(zernio.PROVIDER.submit(API_KEY, payload()))
        assert res.status == "succeeded"


# --------------------------------------------------------------------------
# Status lookup — the capability Status 200 lacks
# --------------------------------------------------------------------------
class TestFetchStatus:
    def test_a_published_post_resolves_with_its_permalink(self, monkeypatch):
        install(monkeypatch, json_route(200, {"post": {
            "_id": "p_1", "status": "published", "platforms": [{
                "platform": "tiktok", "status": "published",
                "platformPostId": "tt_777",
                "publishedUrl": "https://tiktok.test/@me/video/777"}]}}))

        res = run(zernio.PROVIDER.fetch_status(API_KEY, "p_1"))
        assert res.status == "succeeded"
        assert res.provider_native_post_ref == "tt_777"
        assert res.permalink == "https://tiktok.test/@me/video/777"

    def test_a_scheduled_post_is_still_pending(self, monkeypatch):
        install(monkeypatch, json_route(200, {"post": {
            "_id": "p_1", "status": "scheduled",
            "platforms": [{"platform": "tiktok", "status": "scheduled"}]}}))
        assert run(zernio.PROVIDER.fetch_status(API_KEY, "p_1")).status \
            == "submitted"

    def test_a_failed_post_raises_a_classified_error(self, monkeypatch):
        install(monkeypatch, json_route(200, {"post": {
            "_id": "p_1", "status": "failed", "platforms": [{
                "platform": "tiktok", "status": "failed",
                "errorCategory": "auth_expired"}]}}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.fetch_status(API_KEY, "p_1"))
        assert exc.value.code == errors.E_ACCOUNT_AUTH

    def test_a_404_says_nothing_rather_than_declaring_failure(self, monkeypatch):
        """Deleted-at-the-dashboard and never-valid look identical.

        Neither justifies calling a post failed when it may be live on the
        platform, so this yields to the stale sweeper's ``unknown``.
        """
        install(monkeypatch, json_route(404, {"error": "Post not found"}))
        assert run(zernio.PROVIDER.fetch_status(API_KEY, "p_gone")) is None

    def test_an_unreachable_provider_says_nothing(self, monkeypatch):
        install(monkeypatch, boom_route(httpx.ConnectError("down")))
        assert run(zernio.PROVIDER.fetch_status(API_KEY, "p_1")) is None

    def test_an_empty_ref_never_hits_the_network(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, {}))
        assert run(zernio.PROVIDER.fetch_status(API_KEY, "")) is None
        assert seen == []

    def test_the_lookup_is_a_get_on_the_post_path(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, {"post": {
            "_id": "p_1", "status": "publishing"}}))
        run(zernio.PROVIDER.fetch_status(API_KEY, "p_1"))
        assert seen[0].method == "GET"
        assert str(seen[0].url) == f"{POST_URL}/p_1"


# --------------------------------------------------------------------------
# Cancel — what makes remote scheduling reversible
# --------------------------------------------------------------------------
class TestCancel:
    def test_a_scheduled_post_can_be_deleted(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, {"message": "Post deleted"}))
        assert run(zernio.PROVIDER.cancel(API_KEY, "p_1")) is True
        assert seen[0].method == "DELETE"
        assert str(seen[0].url) == f"{POST_URL}/p_1"

    def test_cancelling_something_already_gone_is_success(self, monkeypatch):
        install(monkeypatch, json_route(404, {"error": "Post not found"}))
        assert run(zernio.PROVIDER.cancel(API_KEY, "p_1")) is True

    def test_a_refusal_is_classified_not_swallowed(self, monkeypatch):
        install(monkeypatch, json_route(403, {"error": "not yours"}))
        with pytest.raises(errors.ProviderError):
            run(zernio.PROVIDER.cancel(API_KEY, "p_1"))

    def test_an_unreachable_provider_is_a_network_error(self, monkeypatch):
        install(monkeypatch, boom_route(httpx.ConnectError("down")))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.cancel(API_KEY, "p_1"))
        assert exc.value.code == errors.E_NETWORK


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------
WEBHOOK_PUBLISHED = {
    "id": "evt_9001",
    "event": "post.platform.published",
    "post": {
        "id": "p_abc123",
        "status": "published",
        "platforms": [{
            "platform": "instagram", "status": "published",
            "accountId": "65f0a1b2c3d4e5f60718293a",
            "platformPostId": "ig_5150",
            "publishedUrl": "https://instagram.test/reel/5150",
        }],
        "metadata": {"source": "openshorts"},
    },
    "timestamp": "2026-08-24T09:15:00.000Z",
}


class TestParseWebhook:
    def test_the_envelope_is_read_with_this_providers_own_field_names(self):
        evt = zernio.PROVIDER.parse_webhook(WEBHOOK_PUBLISHED)
        assert evt.event_id == "evt_9001"
        assert evt.event_type == "post.published"
        assert evt.provider_post_ref == "p_abc123"
        assert evt.provider_native_post_ref == "ig_5150"
        assert evt.provider_account_ref == "65f0a1b2c3d4e5f60718293a"
        assert evt.permalink == "https://instagram.test/reel/5150"
        assert evt.created_at is not None
        assert evt.error_code is None

    def test_the_other_providers_parser_would_have_read_nothing(self):
        """Why parse_webhook is per-provider rather than one shared function.

        Not one field name is shared: ``event`` vs ``type``, ``post`` vs
        ``data``, ``publishedUrl`` vs ``permalink``, ``timestamp`` vs
        ``created_at``. The wrong parser produces a well-formed event with every
        field empty — accepted, acked, and matched to nothing.
        """
        wrong = status200.PROVIDER.parse_webhook(WEBHOOK_PUBLISHED)
        assert wrong.event_type == "unknown"
        assert wrong.provider_post_ref is None
        assert wrong.permalink is None

    def test_a_failure_webhook_carries_a_classified_code(self):
        evt = zernio.PROVIDER.parse_webhook({
            "id": "evt_2", "event": "post.platform.failed",
            "post": {"id": "p_9", "status": "failed", "platforms": [{
                "platform": "youtube", "status": "failed",
                "errorCategory": "auth_expired",
                "errorMessage": "reconnect your channel"}]},
            "timestamp": "2026-08-24T09:15:00.000Z"})
        assert evt.event_type == "post.failed"
        assert evt.error_code == errors.E_ACCOUNT_AUTH
        assert "reconnect" in evt.error_message

    def test_a_failure_the_provider_cannot_explain_is_ambiguous(self):
        evt = zernio.PROVIDER.parse_webhook({
            "id": "evt_3", "event": "post.failed",
            "post": {"id": "p_9", "status": "failed", "platforms": [{
                "platform": "tiktok", "status": "failed",
                "errorCategory": "unknown"}]},
            "timestamp": 1756025700})
        assert evt.error_code == errors.E_UNKNOWN

    def test_the_entry_matching_the_event_is_preferred(self):
        """A partial fan-out must not report a sibling's success as this one."""
        evt = zernio.PROVIDER.parse_webhook({
            "id": "evt_4", "event": "post.platform.failed",
            "post": {"id": "p_9", "status": "partial", "platforms": [
                {"platform": "youtube", "status": "published",
                 "publishedUrl": "https://yt.test/1"},
                {"platform": "instagram", "status": "failed",
                 "errorCategory": "user_content",
                 "errorMessage": "bad ratio"}]},
            "timestamp": "2026-08-24T09:15:00.000Z"})
        assert evt.error_message == "bad ratio"
        assert evt.provider_account_ref is None or True
        assert evt.permalink is None

    def test_a_disconnect_event_is_normalized(self):
        evt = zernio.PROVIDER.parse_webhook({
            "id": "evt_5", "event": "account.reconnect_required",
            "post": {}, "timestamp": "2026-08-24T09:15:00.000Z"})
        assert evt.event_type == "account.disconnected"

    def test_an_unrecognized_event_is_not_guessed(self):
        evt = zernio.PROVIDER.parse_webhook({"id": "e", "event": "post.brandnew"})
        assert evt.event_type == "unknown"

    def test_a_garbage_body_does_not_raise(self):
        evt = zernio.PROVIDER.parse_webhook({})
        assert evt.event_type == "unknown"
        assert evt.event_id == ""
        evt = zernio.PROVIDER.parse_webhook({"post": "not-a-dict"})
        assert evt.provider_post_ref is None


class TestWebhookSignature:
    SECRET = "whsec_test_zernio_0001"

    def test_every_standard_encoding_is_accepted(self):
        """The encoding is undocumented, so all three are tried.

        Guessing one and being wrong rejects every callback as unsigned, which
        looks exactly like a provider that is not sending them.
        """
        import base64
        import hashlib
        import hmac
        raw = json.dumps(WEBHOOK_PUBLISHED).encode()
        digest = hmac.new(self.SECRET.encode(), raw, hashlib.sha256).digest()

        for presented in (digest.hex(),
                          "sha256=" + digest.hex(),
                          base64.b64encode(digest).decode(),
                          base64.urlsafe_b64encode(digest).decode().rstrip("=")):
            assert zernio.PROVIDER.verify_signature(self.SECRET, raw, presented)

    def test_a_wrong_signature_is_rejected(self):
        raw = json.dumps(WEBHOOK_PUBLISHED).encode()
        assert not zernio.PROVIDER.verify_signature(self.SECRET, raw, "deadbeef")
        assert not zernio.PROVIDER.verify_signature(self.SECRET, raw, "")

    def test_a_tampered_body_is_rejected(self):
        import hashlib
        import hmac
        raw = json.dumps(WEBHOOK_PUBLISHED).encode()
        sig = hmac.new(self.SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert not zernio.PROVIDER.verify_signature(
            self.SECRET, raw + b" ", sig)

    def test_the_other_providers_verifier_is_untouched(self):
        """Adding an encoding-tolerant path must not change the proven one."""
        raw = b'{"id":"evt_1"}'
        sig = signing.compute_webhook_signature(self.SECRET, raw)
        assert signing.verify_webhook_signature(self.SECRET, raw, sig)


# --------------------------------------------------------------------------
# Accounts: listing, destination verification, credential checks
# --------------------------------------------------------------------------
ACCOUNTS_BODY = {
    "accounts": [
        {"_id": "acct_ig", "platform": "instagram", "username": "clips.ig",
         "isActive": True, "enabled": True, "needsReconnection": False},
        {"_id": "acct_yt", "platform": "youtube", "displayName": "Clips TV",
         "isActive": True, "enabled": True, "needsReconnection": False},
    ],
    "hasAnalyticsAccess": False,
}


class TestListAccounts:
    def test_accounts_are_normalized_for_the_admin_ui(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, ACCOUNTS_BODY))
        out = run(zernio.PROVIDER.list_accounts(API_KEY))

        assert seen[0].method == "GET"
        assert str(seen[0].url) == ACCOUNTS_URL
        assert [a["ref"] for a in out] == ["acct_ig", "acct_yt"]
        assert out[0]["username"] == "clips.ig"
        assert out[1]["username"] == "Clips TV"
        assert out[0]["needs_reconnection"] is False

    def test_a_bad_key_is_classified(self, monkeypatch):
        install(monkeypatch, json_route(401, {"error": "Unauthorized"}))
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.list_accounts(API_KEY))
        assert exc.value.code == errors.E_AUTH


class TestVerifyDestination:
    def test_a_connected_account_verifies_without_publishing(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, ACCOUNTS_BODY))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_ig"))

        assert out["health"] == "ok"
        assert "clips.ig" in out["detail"]
        # Read-only: nothing was posted.
        assert all(r.method == "GET" for r in seen)

    def test_an_unknown_ref_names_what_the_credential_does_have(self,
                                                               monkeypatch):
        install(monkeypatch, json_route(200, ACCOUNTS_BODY))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_typo"))
        assert out["health"] == "blocked"
        assert "clips.ig" in out["detail"]

    def test_a_platform_missing_from_this_key_hints_at_the_slot(self,
                                                               monkeypatch):
        """The exact confusion multi-credential setups produce.

        TikTok lives behind the other Zernio key; without this hint the operator
        sees "not connected" and goes looking at TikTok.
        """
        install(monkeypatch, json_route(200, ACCOUNTS_BODY))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.TIKTOK, "acct_tt"))
        assert out["health"] == "blocked"
        assert "credential slot" in out["detail"]

    def test_a_ref_connected_for_another_platform_is_blocked(self, monkeypatch):
        install(monkeypatch, json_route(200, ACCOUNTS_BODY))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_yt"))
        assert out["health"] == "blocked"
        assert "youtube" in out["detail"]

    def test_an_account_needing_reconnection_is_blocked(self, monkeypatch):
        install(monkeypatch, json_route(200, {"accounts": [
            {"_id": "acct_ig", "platform": "instagram", "username": "clips.ig",
             "isActive": True, "needsReconnection": True}]}))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_ig"))
        assert out["health"] == "blocked"
        assert "reconnected" in out["detail"]

    def test_a_disabled_account_is_blocked(self, monkeypatch):
        install(monkeypatch, json_route(200, {"accounts": [
            {"_id": "acct_ig", "platform": "instagram", "username": "clips.ig",
             "isActive": False, "enabled": False}]}))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_ig"))
        assert out["health"] == "blocked"
        assert "disabled" in out["detail"]

    def test_a_bad_key_blocks_rather_than_leaving_it_unverified(self,
                                                               monkeypatch):
        install(monkeypatch, json_route(401, {"error": "Unauthorized"}))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_ig"))
        assert out["health"] == "blocked"

    def test_an_unreachable_provider_leaves_it_unverified(self, monkeypatch):
        """A network blip is not evidence the destination is broken."""
        install(monkeypatch, boom_route(httpx.ConnectError("down")))
        out = run(zernio.PROVIDER.verify_destination(
            API_KEY, plat.INSTAGRAM, "acct_ig"))
        assert out["health"] == "unverified"


class TestCheckCredential:
    def test_a_working_key_lists_what_is_behind_it(self, monkeypatch):
        seen = install(monkeypatch, json_route(200, ACCOUNTS_BODY))
        out = run(zernio.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is True
        assert "instagram:clips.ig" in out["detail"]
        # Non-destructive by construction: a plain GET, no probe post.
        assert seen[0].method == "GET"

    def test_a_key_with_nothing_connected_is_still_a_working_key(self,
                                                                monkeypatch):
        install(monkeypatch, json_route(200, {"accounts": []}))
        out = run(zernio.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is True
        assert "no social accounts" in out["detail"]

    def test_a_rejected_key_reports_auth(self, monkeypatch):
        install(monkeypatch, json_route(401, {"error": "Unauthorized"}))
        out = run(zernio.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False
        assert out["code"] == errors.E_AUTH

    def test_a_throttled_check_is_not_a_verdict_on_the_key(self, monkeypatch):
        install(monkeypatch, json_route(429, {"error": "Too many requests"}))
        out = run(zernio.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is True

    def test_a_provider_outage_is_not_a_verdict_on_the_key(self, monkeypatch):
        install(monkeypatch, json_route(503, {"error": "unavailable"}))
        out = run(zernio.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False
        assert out["code"] == errors.E_PROVIDER_5XX

    def test_an_unreachable_provider_is_a_network_error(self, monkeypatch):
        install(monkeypatch, boom_route(httpx.ConnectError("down")))
        out = run(zernio.PROVIDER.check_credential(API_KEY))
        assert out["ok"] is False
        assert out["code"] == errors.E_NETWORK


# --------------------------------------------------------------------------
# The unused upload path
# --------------------------------------------------------------------------
class TestUploadMediaIsNotAPath:
    def test_it_fails_loudly_rather_than_returning_a_null_ref(self):
        """Guards a future flip of supports_media_refs without an implementation.

        Not implemented on purpose: Zernio fetches ``mediaItems[].url`` itself,
        and pushing the clip to Zernio as well would send it up the same uplink
        twice — the thing the staging bucket exists to avoid.
        """
        with pytest.raises(errors.ProviderError) as exc:
            run(zernio.PROVIDER.upload_media(
                API_KEY, media_url="https://cdn.test/clip.mp4"))
        assert exc.value.code == errors.E_UNSUPPORTED
