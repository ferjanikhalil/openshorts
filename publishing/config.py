"""Configuration for the publishing subsystem.

Read lazily from the environment so importing this package has no side effects
(same contract as cloud/config.py). Nothing here loads unless
``PUBLISHING_ENABLED`` is truthy.
"""
import base64
import os


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def is_enabled() -> bool:
    """Master switch. When False the whole publishing package stays dormant."""
    return _flag("PUBLISHING_ENABLED")


# --- Operational defaults ---------------------------------------------------
# Retry/backoff. Deliberately generous: a social API outage should not burn
# through all attempts in five minutes.
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600

# An attempt handed to the provider but never confirmed by a webhook is moved to
# `unknown` after this long. NEVER auto-retried — a blind retry on a post that
# may already be live double-publishes to a real audience.
SUBMIT_TIMEOUT_SECONDS = 1800

# Status polling, for the providers that support it at all.
#
# `supports_status_lookup` is False for Status 200 (no such endpoint), so for
# years the ONLY completion signal was a webhook and the timeout above was the
# only backstop. A provider that can be asked "is this post live?" turns that
# backstop into a last resort instead of the normal ending for any post whose
# callback was lost.
#
# The floor exists because polling immediately is both useless and rude: the
# provider has just accepted the post and is still working on it, and a poll at
# t+2s spends a request to be told what the submit response already said. It also
# has to stay well under SUBMIT_TIMEOUT_SECONDS, or a post would be condemned to
# `unknown` before it was ever asked about.
STATUS_POLL_MIN_AGE_SECONDS = 120
# Re-ask no more often than this. Reconciliation runs every 60s by default, and a
# post that stays pending for an hour must not cost 60 requests.
STATUS_POLL_INTERVAL_SECONDS = 300
# Ceiling per pass. Polling is serial provider I/O inside one transaction, so an
# unbounded batch would hold a transaction open for minutes.
STATUS_POLL_BATCH = 20

# How old a claim must be before boot recovery treats it as abandoned.
#
# `in_flight` means "claimed, not yet handed to the provider", and recovery
# re-queues those rows. With ONE long-lived process that is unambiguous: nothing
# is in flight at boot. With two — the app host and the always-on publisher share
# this queue by design — a recovery pass can meet a claim that a *live* worker is
# still working through, and re-queuing that one is how the same clip gets posted
# twice.
#
# The bound is derived from how long a claim can legitimately live: one dispatch
# is at most a media registration plus a submit, each capped by the provider
# client at 10s connect + 120s read, so ~260s. `dispatch_once` then works its
# claimed batch serially, so with the default limit of 10 the last row in a batch
# can sit in flight far longer than that. 15 minutes covers a single dispatch
# several times over without waiting out a whole batch.
#
# It is a thrash-avoidance knob, NOT the safety boundary. The boundary is the
# claim-ownership check in `dispatcher.dispatch_attempt`, which is what makes an
# early recovery cost a wasted claim instead of a duplicate post.
ORPHAN_CLAIM_MIN_AGE_SECONDS = 900

# Provider media refs expire (Status 200: 7 days). Refresh a ref this long
# before its stated expiry so a scheduled post never submits a dead ref.
MEDIA_REFRESH_MARGIN_SECONDS = 3600

# Lifetime of the media URL given to the provider. It keeps that URL and fetches
# from it at POST time, so it must outlive the ref above — not the HTTP request
# that created it. 7 days is also SigV4's maximum for a presigned URL.
DEFAULT_PROVIDER_MEDIA_URL_TTL_SECONDS = 7 * 24 * 3600

# Minimum wall-clock gap between two provider media registrations. The provider
# ingests media by pulling the whole clip from OUR origin, so back-to-back
# registrations are back-to-back multi-hundred-MB downloads through one tunnel
# or uplink — the congestion spiral observed 2026-08-15. One per gap is the
# ceiling a self-hosted origin can actually serve.
DEFAULT_MEDIA_REGISTRATION_GAP_SECONDS = 90

# Staging clips in an object store (publishing/objectstore.py). The transfer loop
# polls this often for work; a pass that moves a clip loops again immediately, so
# this is idle latency, not throughput.
DEFAULT_TRANSFER_INTERVAL_SECONDS = 15
# How long a staged object outlives the last attempt that needs it. Objects for
# posts still queued are never swept regardless of age; this only governs the
# tail, and it is what keeps a free-tier bucket from filling up.
DEFAULT_STORE_RETENTION_HOURS = 48
# The retention sweep is a list + a handful of deletes. Once per half hour is
# plenty and keeps the free tiers' request counts irrelevant.
STORE_SWEEP_INTERVAL_SECONDS = 1800

# Remote scheduling delivery. "auto" (default) hands a scheduled post to the
# provider immediately together with its future timestamp, and falls back to
# the local clock only if the live API rejects the field; "off" always holds
# the clock locally and submits at the appointed time.
DEFAULT_REMOTE_SCHEDULE_MODE = "auto"

# Webhook events carrying a created_at outside this window are rejected. The
# signature alone has no timestamp/nonce, so it never expires on its own — this
# window is the only replay bound.
WEBHOOK_MAX_SKEW_SECONDS = 900

# Terminal publish records are swept after this long.
RETENTION_SECONDS = 90 * 24 * 3600


class Settings:
    """Lazily-evaluated env-backed settings. Access attributes, not the class."""

    @property
    def database_url(self) -> str:
        return os.environ.get("DATABASE_URL", "")

    # --- Credential encryption ---
    @property
    def master_key_b64(self) -> str:
        return os.environ.get("PUBLISHING_MASTER_KEY", "")

    @property
    def master_key_old_b64(self) -> str:
        """Previous master key, kept readable during a rotation window."""
        return os.environ.get("PUBLISHING_MASTER_KEY_OLD", "")

    # --- Admin access ---
    @property
    def admin_emails(self) -> list:
        raw = os.environ.get("PUBLISHING_ADMIN_EMAILS", "")
        return [e.strip().lower() for e in raw.split(",") if e.strip()]

    @property
    def admin_token(self) -> str:
        # Stripped, exactly like admin_emails above. This value gets pasted into a
        # host's environment UI — Render's is a multi-line textarea — where a
        # trailing newline is invisible on screen and fatal in effect: _check_token
        # uses hmac.compare_digest, an exact byte match, so the operator compares
        # the two values, sees them agree character for character, and cannot
        # explain the 401. Cost hours on 2026-08-22. Whitespace around a service
        # credential is never intentional.
        return os.environ.get("PUBLISHING_ADMIN_TOKEN", "").strip()

    # --- Media reachability ---
    @property
    def public_base_url(self) -> str:
        """Public origin the PROVIDER must be able to reach to fetch media.

        Status 200 ingests media by URL, so a clip has to be fetchable from the
        public internet. Falls back to FRONTEND_URL, which is right for a normal
        single-origin deploy.
        """
        return (os.environ.get("PUBLISHING_PUBLIC_BASE_URL")
                or os.environ.get("FRONTEND_URL", "")).rstrip("/")

    @property
    def media_url_ttl_seconds(self) -> int:
        return int(os.environ.get("PUBLISHING_MEDIA_URL_TTL", "3600"))

    @property
    def provider_media_url_ttl_seconds(self) -> int:
        """TTL for a URL handed to the PROVIDER, which is a different lifetime.

        Status 200 does not copy the bytes at registration — it keeps the URL and
        downloads from it when the post actually goes out, which for a scheduled
        post is hours or days later. So this URL has to outlive the media ref
        (7 days), not the request that created it. A one-hour URL here is a post
        that fails at its slot with "could not download", long after everything
        looked fine.

        Clamped to SigV4's 7-day ceiling; a longer value would produce presigned
        URLs the store rejects outright.
        """
        raw = int(os.environ.get("PUBLISHING_PROVIDER_MEDIA_URL_TTL",
                                 str(DEFAULT_PROVIDER_MEDIA_URL_TTL_SECONDS)))
        return max(3600, min(raw, 7 * 24 * 3600))

    # --- Concurrency ---
    @property
    def max_concurrent_uploads(self) -> int:
        return int(os.environ.get("PUBLISHING_MAX_CONCURRENT_UPLOADS", "4"))

    @property
    def per_credential_concurrency(self) -> int:
        """Provider quotas are per account, so one slow/throttled credential must
        not stall the other groups."""
        return int(os.environ.get("PUBLISHING_PER_CREDENTIAL_CONCURRENCY", "1"))

    @property
    def max_attempts(self) -> int:
        return int(os.environ.get("PUBLISHING_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS)))

    @property
    def dispatch_interval_seconds(self) -> float:
        return float(os.environ.get("PUBLISHING_DISPATCH_INTERVAL", "5"))

    @property
    def reconcile_interval_seconds(self) -> float:
        return float(os.environ.get("PUBLISHING_RECONCILE_INTERVAL", "60"))

    @property
    def submit_timeout_seconds(self) -> int:
        return int(os.environ.get("PUBLISHING_SUBMIT_TIMEOUT",
                                 str(SUBMIT_TIMEOUT_SECONDS)))

    @property
    def status_poll_min_age_seconds(self) -> int:
        """How long a submitted post is left alone before it is polled."""
        return int(os.environ.get("PUBLISHING_STATUS_POLL_MIN_AGE",
                                  str(STATUS_POLL_MIN_AGE_SECONDS)))

    @property
    def status_poll_interval_seconds(self) -> int:
        """Minimum gap between two polls of the same post."""
        return int(os.environ.get("PUBLISHING_STATUS_POLL_INTERVAL",
                                  str(STATUS_POLL_INTERVAL_SECONDS)))

    @property
    def status_poll_batch(self) -> int:
        return int(os.environ.get("PUBLISHING_STATUS_POLL_BATCH",
                                  str(STATUS_POLL_BATCH)))

    @property
    def status_poll_enabled(self) -> bool:
        """Off switch for the poller, independent of the provider's capability.

        The kill switch for the case where polling itself is the problem — a
        provider rate-limiting the status endpoint, or answering it wrongly.
        Webhooks and the stale sweeper still resolve posts without it.
        """
        return os.environ.get(
            "PUBLISHING_STATUS_POLL", "true").strip().lower() not in (
                "0", "false", "no", "off")

    @property
    def orphan_claim_min_age_seconds(self) -> int:
        """Age at which boot recovery may re-queue an ``in_flight`` claim.

        Lower means a claim abandoned by a killed process is retried sooner;
        higher means less chance of a recovery pass colliding with a worker that
        is still mid-batch. See ORPHAN_CLAIM_MIN_AGE_SECONDS for the derivation.
        """
        return int(os.environ.get("PUBLISHING_ORPHAN_CLAIM_MIN_AGE",
                                  str(ORPHAN_CLAIM_MIN_AGE_SECONDS)))

    @property
    def media_registration_gap_seconds(self) -> float:
        return float(os.environ.get(
            "PUBLISHING_MEDIA_REGISTRATION_GAP",
            str(DEFAULT_MEDIA_REGISTRATION_GAP_SECONDS)))

    # --- Object-store staging ---
    @property
    def transfer_interval_seconds(self) -> float:
        return float(os.environ.get("PUBLISHING_TRANSFER_INTERVAL",
                                    str(DEFAULT_TRANSFER_INTERVAL_SECONDS)))

    @property
    def store_retention_hours(self) -> float:
        """0 disables the sweep — for a bucket with its own lifecycle rule."""
        return float(os.environ.get("PUBLISHING_STORE_RETENTION_HOURS",
                                    str(DEFAULT_STORE_RETENTION_HOURS)))

    @property
    def remote_schedule_mode(self) -> str:
        mode = os.environ.get("PUBLISHING_REMOTE_SCHEDULE",
                              DEFAULT_REMOTE_SCHEDULE_MODE).strip().lower()
        return mode if mode in ("auto", "on", "off") else "auto"

    @property
    def role(self) -> str:
        """``full`` (default) or ``publisher``. Cosmetic to dispatch, not to health.

        A publisher-only instance is the always-on half of a split deployment: it
        shares the database but holds no clip files, because the machine that
        generates clips may be asleep at a slot. Everything that matters about
        that difference is already self-describing at runtime — no clip resolver
        is registered, and the dispatcher answers from ``publish_media`` instead
        (see ``dispatcher._staged_info``). The one thing that is NOT
        self-describing is whether the absence is intentional: a full instance
        with no resolver is broken, and a publisher with no resolver is correct.
        Health has to tell those apart, so the operator declares it.
        """
        role = os.environ.get("PUBLISHING_ROLE", "full").strip().lower()
        return role if role in ("full", "publisher") else "full"

    # --- Provider selection ---
    @property
    def default_provider(self) -> str:
        return os.environ.get("PUBLISHING_DEFAULT_PROVIDER", "status200")

    @property
    def dry_run(self) -> bool:
        """Route submits to the in-repo fake provider instead of a live API.

        Lets the full pipeline (queue, retries, webhooks, UI) be exercised
        end-to-end with no real credential and no real post.
        """
        return _flag("PUBLISHING_DRY_RUN")


settings = Settings()


def decode_master_key(raw_b64: str) -> bytes:
    """Decode a base64 master key and enforce AES-256 length."""
    try:
        key = base64.b64decode(raw_b64, validate=True)
    except Exception as e:
        raise RuntimeError(
            "PUBLISHING_MASTER_KEY is not valid base64. Generate one with: "
            "python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\""
        ) from e
    if len(key) != 32:
        raise RuntimeError(
            f"PUBLISHING_MASTER_KEY must decode to exactly 32 bytes (got {len(key)}). "
            "Generate one with: python -c \"import os,base64;"
            "print(base64.b64encode(os.urandom(32)).decode())\""
        )
    return key


def validate_required():
    """Fail fast on a misconfigured deploy instead of at first publish.

    Publishing REQUIRES Postgres: duplicate-post prevention, retry state and the
    audit trail are durability requirements, and an in-memory guard that forgets
    on redeploy is not a guard.
    """
    missing = []
    if not settings.database_url:
        missing.append("DATABASE_URL")
    if not settings.master_key_b64:
        missing.append("PUBLISHING_MASTER_KEY")
    if missing:
        raise RuntimeError(
            "PUBLISHING_ENABLED is set but required settings are missing: "
            + ", ".join(missing)
            + ". Publishing needs Postgres (durable dedupe/retry/audit state) and "
            "a master key to encrypt provider credentials. Unset PUBLISHING_ENABLED "
            "to run without publishing."
        )
    # Validates length/encoding — raises with generation instructions if wrong.
    decode_master_key(settings.master_key_b64)
    if settings.master_key_old_b64:
        decode_master_key(settings.master_key_old_b64)
