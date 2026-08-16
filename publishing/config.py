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

# Provider media refs expire (Status 200: 7 days). Refresh a ref this long
# before its stated expiry so a scheduled post never submits a dead ref.
MEDIA_REFRESH_MARGIN_SECONDS = 3600

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
        return os.environ.get("PUBLISHING_ADMIN_TOKEN", "")

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
