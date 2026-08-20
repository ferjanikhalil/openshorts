"""Pure-logic tests for the media origin: which strategy, and which key.

No network, no boto3 calls, no credentials — the object store is stubbed at the
``publishing.objectstore`` seam. What is verified here is the part that decides
whether a real post can go out at all: the strategy ladder, the content-addressed
key, and above all that a clip which has not finished uploading yet produces a
WAIT (park) and never a failure.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from publishing import dispatcher, errors, media, objectstore
from publishing.config import MEDIA_REFRESH_MARGIN_SECONDS


def _utc():
    return datetime.now(timezone.utc)

S3_VARS = ("PUBLISHING_S3_ENDPOINT", "PUBLISHING_S3_BUCKET",
           "PUBLISHING_S3_REGION", "PUBLISHING_S3_ACCESS_KEY_ID",
           "PUBLISHING_S3_SECRET_ACCESS_KEY",
           "R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID",
           "R2_SECRET_ACCESS_KEY",
           "AWS_S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
           "AWS_REGION")


@pytest.fixture
def clean_env(monkeypatch):
    """Start from a deploy with no object storage of any kind."""
    for name in S3_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PUBLISHING_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    # R2 lives in cloud.config, which media reads through r2_available().
    monkeypatch.setattr(media, "r2_available", lambda: False)
    return monkeypatch


def _configure_store(monkeypatch, **over):
    values = {
        "PUBLISHING_S3_ENDPOINT": "https://proj.storage.supabase.co/storage/v1/s3",
        "PUBLISHING_S3_BUCKET": "clips",
        "PUBLISHING_S3_REGION": "eu-central-1",
        "PUBLISHING_S3_ACCESS_KEY_ID": "akid",
        "PUBLISHING_S3_SECRET_ACCESS_KEY": "secret",
    }
    values.update(over)
    for k, v in values.items():
        monkeypatch.setenv(k, v)


class TestStoreConfig:
    def test_unconfigured_by_default(self, clean_env):
        assert objectstore.config() is None
        assert objectstore.configured() is False
        assert objectstore.describe()["configured"] is False

    def test_publishing_vars_win_over_aws(self, clean_env):
        _configure_store(clean_env)
        clean_env.setenv("AWS_S3_BUCKET", "backups")
        clean_env.setenv("AWS_ACCESS_KEY_ID", "other")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "other")
        cfg = objectstore.config()
        assert (cfg.source, cfg.bucket, cfg.region) == (
            "PUBLISHING_S3", "clips", "eu-central-1")

    def test_falls_back_to_the_s3_backup_keys(self, clean_env):
        # A deploy that already has S3 backup configured gets a fast media
        # origin without setting anything new.
        clean_env.setenv("AWS_S3_BUCKET", "backups")
        clean_env.setenv("AWS_ACCESS_KEY_ID", "akid")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        clean_env.setenv("AWS_REGION", "us-west-2")
        cfg = objectstore.config()
        assert (cfg.source, cfg.bucket, cfg.endpoint, cfg.region) == (
            "AWS", "backups", "", "us-west-2")

    def test_a_partial_credential_set_is_not_configured(self, clean_env):
        # Half-configured must read as absent: "configured" gates whether posts
        # park waiting for an upload that could never happen.
        clean_env.setenv("PUBLISHING_S3_BUCKET", "clips")
        clean_env.setenv("PUBLISHING_S3_ACCESS_KEY_ID", "akid")
        assert objectstore.config() is None

    def test_region_defaults(self, clean_env):
        _configure_store(clean_env, PUBLISHING_S3_REGION="")
        assert objectstore.config().region == "auto"  # custom endpoint
        _configure_store(clean_env, PUBLISHING_S3_ENDPOINT="",
                         PUBLISHING_S3_REGION="")
        assert objectstore.config().region == "us-east-1"  # real AWS

    def test_describe_never_leaks_key_material(self, clean_env):
        _configure_store(clean_env)
        blob = repr(objectstore.describe())
        assert "akid" not in blob and "secret" not in blob
        assert objectstore.describe()["endpoint"] == "proj.storage.supabase.co"


class TestObjectKey:
    def test_key_is_content_addressed(self):
        key = objectstore.object_key("job-1", 2, "fp123")
        assert key == "publishing/job-1/2/fp123.mp4"

    def test_a_new_fingerprint_is_a_new_object(self):
        # Re-styling a clip changes size/mtime and therefore the fingerprint.
        # Reusing one key would serve the OLD bytes under a live URL.
        assert (objectstore.object_key("job-1", 0, "before")
                != objectstore.object_key("job-1", 0, "after"))

    def test_path_separators_cannot_escape_the_prefix(self):
        key = objectstore.object_key("../../etc", 0, "../fp")
        assert key.startswith("publishing/")
        assert key.count("/") == 3

    def test_fingerprint_round_trips(self):
        key = objectstore.object_key("job-1", 7, "fp123")
        assert objectstore.key_fingerprint(key) == "fp123"

    def test_foreign_keys_yield_no_fingerprint(self):
        # The sweeper deletes what it cannot account for only if it can parse
        # the key; anything else in the bucket must be invisible to it.
        for key in ("other/thing.mp4", "publishing/a/b.mp4",
                    "publishing/job/0/fp.txt"):
            assert objectstore.key_fingerprint(key) is None


class TestStrategyLadder:
    def test_nothing_configured(self, clean_env):
        assert media.media_strategy() == "none"
        assert media.reachability_warnings()

    def test_signed_token_when_only_a_public_origin_exists(self, clean_env):
        clean_env.setenv("PUBLISHING_PUBLIC_BASE_URL", "https://app.example.com")
        assert media.media_strategy() == "signed_token"
        assert media.reachability_warnings() == []

    def test_store_outranks_the_signed_token_route(self, clean_env):
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        clean_env.setenv("PUBLISHING_PUBLIC_BASE_URL", "https://app.example.com")
        assert media.media_strategy() == "objectstore_presigned"

    def test_r2_outranks_the_store(self, clean_env):
        # In cloud mode the bytes are already in R2; staging a second copy would
        # be pure waste.
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        clean_env.setattr(media, "r2_available", lambda: True)
        assert media.media_strategy() == "r2_presigned"

    def test_store_without_boto3_is_not_a_strategy(self, clean_env):
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: False)
        clean_env.setenv("PUBLISHING_PUBLIC_BASE_URL", "https://app.example.com")
        assert media.media_strategy() == "signed_token"

    def test_store_still_warns_about_a_missing_webhook_origin(self, clean_env):
        # Clips are fine, but with no public base URL the provider has nowhere
        # to confirm to, and every post ages into `unknown`.
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        warnings = media.reachability_warnings()
        assert len(warnings) == 1 and "webhook" in warnings[0]


class TestPublicUrlForClip:
    def _resolve(self, **kw):
        return asyncio.run(media.public_url_for_clip(
            "job-1", 0, "clip_0.mp4", fingerprint="fp123", **kw))

    def test_presigns_a_staged_object(self, clean_env):
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        clean_env.setattr(objectstore, "head",
                          lambda key: {"size_bytes": 1024})
        clean_env.setattr(objectstore, "presigned_get",
                          lambda key, expires=0: f"https://store/{key}?sig=x")
        url, strategy = self._resolve()
        assert strategy == "objectstore_presigned"
        assert url == "https://store/publishing/job-1/0/fp123.mp4?sig=x"

    def test_an_unstaged_clip_is_pending_not_failed(self, clean_env):
        # The single most important case: the transfer loop is mid-upload, so the
        # answer is "wait", and the dispatcher must park without consuming a try.
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        clean_env.setattr(objectstore, "head", lambda key: None)
        url, strategy = self._resolve()
        assert url is None
        assert media.is_pending(strategy)

    def test_an_unreachable_store_parks_rather_than_degrading(self, clean_env):
        # Falling back to the slow route here would hand the provider a URL this
        # uplink cannot serve in time — and a timed-out submit is ambiguous
        # forever. Parking keeps the post held with a visible reason.
        _configure_store(clean_env)
        clean_env.setenv("PUBLISHING_PUBLIC_BASE_URL", "https://app.example.com")
        clean_env.setattr(objectstore, "boto3_available", lambda: True)

        def boom(key):
            raise objectstore.StoreError("NoSuchBucket: clips")

        clean_env.setattr(objectstore, "head", boom)
        url, strategy = self._resolve()
        assert url is None and media.is_pending(strategy)
        assert "NoSuchBucket" in strategy

    def test_signed_token_route_is_unchanged_without_a_store(self, clean_env):
        clean_env.setenv("PUBLISHING_PUBLIC_BASE_URL", "https://app.example.com")
        clean_env.setenv("PUBLISHING_MASTER_KEY",
                         "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        url, strategy = self._resolve()
        assert strategy == "signed_token"
        assert url.startswith(
            "https://app.example.com" + media.MEDIA_ROUTE_PREFIX + "/")

    def test_no_origin_at_all_reports_why(self, clean_env):
        url, strategy = self._resolve()
        assert url is None
        assert not media.is_pending(strategy)  # a real failure, not a wait
        assert "PUBLISHING_S3_" in strategy

    def test_a_clip_with_no_fingerprint_skips_the_store(self, clean_env):
        # The key is content-addressed, so without a fingerprint there is no
        # object to look for; fall through rather than invent a key.
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        clean_env.setenv("PUBLISHING_PUBLIC_BASE_URL", "https://app.example.com")
        clean_env.setenv("PUBLISHING_MASTER_KEY",
                         "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        _url, strategy = asyncio.run(media.public_url_for_clip(
            "job-1", 0, "clip_0.mp4", fingerprint=None))
        assert strategy == "signed_token"


class TestProviderUrlLifetime:
    """The provider keeps the URL and downloads from it at POST time.

    A scheduled post registers media hours or days before the download, so a URL
    scoped to the registering request expires before it is ever used — the post
    then fails at its slot with "could not download the file", which looks like a
    provider fault and is not one.
    """

    def test_default_covers_a_scheduled_week(self, clean_env):
        clean_env.delenv("PUBLISHING_PROVIDER_MEDIA_URL_TTL", raising=False)
        from publishing.config import MEDIA_REFRESH_MARGIN_SECONDS, settings
        ttl = settings.provider_media_url_ttl_seconds
        assert ttl == 7 * 24 * 3600
        # Must outlive the ref it is attached to, refresh margin included.
        assert ttl >= 7 * 24 * 3600 - MEDIA_REFRESH_MARGIN_SECONDS
        # And it is a different lifetime from the short-lived-URL knob.
        assert ttl > settings.media_url_ttl_seconds

    def test_clamped_to_sigv4s_ceiling(self, clean_env):
        from publishing.config import settings
        clean_env.setenv("PUBLISHING_PROVIDER_MEDIA_URL_TTL", "9999999")
        assert settings.provider_media_url_ttl_seconds == 7 * 24 * 3600
        clean_env.setenv("PUBLISHING_PROVIDER_MEDIA_URL_TTL", "5")
        assert settings.provider_media_url_ttl_seconds == 3600

    def test_the_ttl_reaches_the_presigner(self, clean_env):
        _configure_store(clean_env)
        clean_env.setattr(objectstore, "boto3_available", lambda: True)
        clean_env.setattr(objectstore, "head", lambda key: {"size_bytes": 1})
        seen = {}

        def presign(key, expires=0):
            seen["expires"] = expires
            return "https://store/x"

        clean_env.setattr(objectstore, "presigned_get", presign)
        asyncio.run(media.public_url_for_clip(
            "job-1", 0, "clip_0.mp4", fingerprint="fp123", ttl_seconds=604800))
        assert seen["expires"] == 604800


class TestPendingClassification:
    def test_media_pending_is_transient(self):
        # It describes OUR upload, not the provider: it must never look permanent
        # and must never be treated as ambiguous.
        assert errors.E_MEDIA_PENDING in errors.TRANSIENT
        assert errors.E_MEDIA_PENDING not in errors.PERMANENT
        err = errors.ProviderError(errors.E_MEDIA_PENDING, "still uploading")
        assert err.retryable and not err.is_ambiguous

    def test_pending_is_not_confused_with_a_real_strategy(self):
        assert media.is_pending(media.STRATEGY_PENDING)
        assert media.is_pending(media.STRATEGY_PENDING + ": detail")
        for other in ("signed_token", "objectstore_presigned", "r2_presigned",
                      "none", ""):
            assert not media.is_pending(other)


class _StubResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _StubSession:
    """Just enough session for _staged_info: one query, one row."""

    def __init__(self, row):
        self.row = row
        self.queries = 0

    async def execute(self, stmt):
        self.queries += 1
        return _StubResult(self.row)


def _media_row(**over):
    values = {"content_fingerprint": "f" * 64, "size_bytes": 21_700_000,
              "source_url": "https://proj.storage.supabase.co/o/clips/abc.mp4",
              "expires_at": _utc() + timedelta(days=6)}
    values.update(over)
    return SimpleNamespace(**values)


def _staged(row, fingerprint="f" * 64):
    attempt = SimpleNamespace(publish_group_id="g1", provider="status200")
    req = SimpleNamespace(content_fingerprint=fingerprint, job_id="job-1",
                          clip_index=0)
    session = _StubSession(row)
    return asyncio.run(dispatcher._staged_info(session, attempt, req)), session


@pytest.fixture
def no_store(monkeypatch):
    """Default for the DB-only cases: the store answers nothing."""
    monkeypatch.setattr(media, "store_available", lambda: False)
    return monkeypatch


class TestPublisherWithoutClipFiles:
    """The always-on publisher: same database, no clip files on disk.

    This is what makes an off-box publisher possible at all. The machine that
    generates clips may be asleep at the slot, so the submitting process has to
    answer "where are this clip's bytes?" from the media table or the object
    store instead of the filesystem — and, crucially, must PARK anything it
    cannot answer rather than failing it. The guard it bypasses is permanent, so
    getting this wrong does not delay a post, it destroys it.
    """

    def test_a_staged_clip_needs_no_local_file(self, no_store):
        info, session = _staged(_media_row())
        assert info is not None, "a fresh media ref is enough to publish"
        assert session.queries == 1
        assert info["fingerprint"] == "f" * 64
        # Real, so the platform size ceiling is still enforced off-box.
        assert info["size_bytes"] == 21_700_000
        # Non-empty, because the caller reads a falsy filename as "bytes gone".
        assert info["filename"] == "abc.mp4"

    def test_a_ref_that_never_expires_is_fine(self, no_store):
        info, _ = _staged(_media_row(expires_at=None))
        assert info is not None

    def test_an_unstaged_clip_parks_instead_of_failing(self, no_store):
        info, session = _staged(None)
        assert info is None, "None means park; the owning instance can stage it"
        assert session.queries == 1

    def test_a_ref_about_to_expire_parks(self, no_store):
        # Refreshing a ref means re-uploading, which needs bytes this process
        # does not have. Submitting it anyway would hand the provider a URL that
        # is dead by the time the post actually goes out.
        row = _media_row(expires_at=_utc() + timedelta(
            seconds=MEDIA_REFRESH_MARGIN_SECONDS - 30))
        assert _staged(row)[0] is None

    def test_an_expired_ref_parks(self, no_store):
        row = _media_row(expires_at=_utc() - timedelta(hours=1))
        assert _staged(row)[0] is None

    def test_no_fingerprint_asks_the_database_nothing(self, no_store):
        info, session = _staged(_media_row(), fingerprint=None)
        assert info is None
        assert session.queries == 0, "nothing to look a media ref up by"

    def test_a_ref_with_no_source_url_still_yields_a_filename(self, no_store):
        info, _ = _staged(_media_row(source_url=None))
        assert info["filename"] == "staged-clip"

    def test_the_wait_is_long_enough_to_be_a_park(self):
        # Seconds would spin the queue against an upload measured in minutes.
        assert dispatcher.STAGING_WAIT_SECONDS >= 60


class TestPublisherRegistersItsOwnMedia:
    """With bytes in the bucket but no ref yet, the publisher does not wait.

    Registering a ref is presign + one provider call, and neither reads a local
    byte — so requiring the app host to have pre-registered would put it back on
    the critical path for every slot, which is the exact dependency this whole
    split deployment exists to remove. The app host stages; this host does the
    rest.
    """

    def _with_store(self, monkeypatch, head):
        monkeypatch.setattr(media, "store_available", lambda: True)
        monkeypatch.setattr(objectstore, "head", head)

    def test_a_staged_object_is_enough(self, monkeypatch):
        seen = {}

        def head(key):
            seen["key"] = key
            return {"size_bytes": 18_400_000}

        self._with_store(monkeypatch, head)
        info, _ = _staged(None)
        assert info is not None, "the bytes are in the bucket; publish them"
        # Content-addressed, so it must be the key the transfer loop wrote.
        assert seen["key"] == objectstore.object_key("job-1", 0, "f" * 64)
        assert info["filename"] == f"{'f' * 64}.mp4"
        assert info["fingerprint"] == "f" * 64
        # From the store, so the ceiling check works before any upload exists.
        assert info["size_bytes"] == 18_400_000

    def test_an_absent_object_parks(self, monkeypatch):
        self._with_store(monkeypatch, lambda key: None)
        assert _staged(None)[0] is None

    def test_an_unreachable_store_parks_and_never_fails(self, monkeypatch):
        # A store we cannot reach says nothing about whether the object exists.
        # Failing here would destroy a post over a transient network fault.
        def boom(key):
            raise objectstore.StoreError("connection reset")

        self._with_store(monkeypatch, boom)
        assert _staged(None)[0] is None

    def test_the_media_row_still_wins(self, monkeypatch):
        # A registered ref is authoritative and cheaper: no HEAD at all.
        def boom(key):  # pragma: no cover - must not be called
            raise AssertionError("the store was consulted despite a live ref")

        self._with_store(monkeypatch, boom)
        info, _ = _staged(_media_row())
        assert info["filename"] == "abc.mp4"

    def test_an_expiring_ref_does_not_fall_through_to_the_store(self, monkeypatch):
        # It must park for a re-upload by the host with the bytes. Registering a
        # fresh ref from the same object would presign it again and post a URL
        # whose object may be swept, so the near-expiry park has to stay a park.
        def boom(key):  # pragma: no cover - must not be called
            raise AssertionError("fell through to the store on a stale ref")

        self._with_store(monkeypatch, boom)
        row = _media_row(expires_at=_utc() + timedelta(
            seconds=MEDIA_REFRESH_MARGIN_SECONDS - 30))
        assert _staged(row)[0] is None

    def test_no_store_configured_parks_without_a_call(self, monkeypatch):
        monkeypatch.setattr(media, "store_available", lambda: False)
        assert _staged(None)[0] is None


class TestPublisherRole:
    """Health has to tell "no clips by design" apart from "resolver broke"."""

    def test_default_role_is_full(self, clean_env):
        from publishing.config import settings
        clean_env.delenv("PUBLISHING_ROLE", raising=False)
        assert settings.role == "full"

    def test_publisher_role_is_recognised(self, clean_env):
        from publishing.config import settings
        clean_env.setenv("PUBLISHING_ROLE", " Publisher ")
        assert settings.role == "publisher"

    def test_an_unknown_role_falls_back_to_full(self, clean_env):
        # Fail toward the stricter health check: a typo must not make a broken
        # app host report itself healthy.
        from publishing.config import settings
        clean_env.setenv("PUBLISHING_ROLE", "wroker")
        assert settings.role == "full"

