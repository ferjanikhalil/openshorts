"""Provider-neutral error taxonomy.

Adapters translate whatever their API returns into these classes. Everything
outside ``providers/`` reasons about publishing failures in these terms only —
that is what keeps ``if provider == "status200"`` out of the dispatcher.

The classification drives one decision: retry, defer, or stop. Getting it wrong
is expensive in both directions. Retrying a non-retryable error burns the daily
quota on a post that can never succeed; treating a transient network blip as
permanent silently drops a publication.
"""
from typing import Optional

# --- Error codes ------------------------------------------------------------
# Permanent — retrying cannot help. A human must change something.
E_AUTH = "auth_invalid"                # bad/revoked API key
# The API key is fine; ONE connected account's platform token expired at the
# provider and that account has to be re-linked. Distinct from E_AUTH because the
# blast radius is different by an order of magnitude: E_AUTH stops every post for
# the group, this stops one destination. Conflating them is what took a whole
# group offline on 2026-08-17 when a single Instagram session expired — every
# platform then hit "no usable credential" and re-parked itself every 15 minutes,
# silently, forever. Wording that means THIS must never be classified as E_AUTH.
E_ACCOUNT_AUTH = "account_reauth_required"
E_NOT_CONNECTED = "not_connected"      # destination not linked at the provider
E_VALIDATION = "validation"            # malformed request / bad caption
E_MEDIA_TOO_LARGE = "media_too_large"  # exceeds the platform ceiling
E_MEDIA_UNFETCHABLE = "media_unfetchable"  # provider could not download the URL
E_UNSUPPORTED = "unsupported"          # platform rejects this media/option
E_DUPLICATE = "duplicate"              # provider says it already has this post
# The provider's PLAN refuses this, not the request: a free tier that connects
# only N social accounts, a feature gated behind an upgrade. Distinct from
# E_AUTH (the key works fine) and from E_NOT_CONNECTED (the account may be
# perfectly linked) because the fix is neither a new key nor a re-link — it is a
# billing or account-topology decision only a human can make. Retrying spends
# quota on a post that cannot succeed until then.
E_PLAN_LIMIT = "plan_limit"

# Transient — retry with backoff.
E_NETWORK = "network"
E_TIMEOUT = "timeout"
E_PROVIDER_5XX = "provider_error"
# Our own media staging has not finished: the clip is still being copied to the
# object store the provider will fetch it from. Nothing has been sent, nothing is
# wrong — the transfer loop is mid-upload. Transient so that any path which does
# not special-case it waits instead of killing the post; the dispatcher parks
# without consuming a try, because a slow uplink must not spend the retry budget.
E_MEDIA_PENDING = "media_pending"

# Capacity — retry, but on the provider's clock, not ours.
E_RATE_LIMITED = "rate_limited"        # spacing cooldown; Retry-After applies
E_QUOTA_EXHAUSTED = "quota_exhausted"  # daily cap; wait for the reset

# Ambiguous — we do not know whether the post went out.
E_UNKNOWN = "unknown"

# The provider refused the remote-schedule FIELD itself (not the post): nothing
# was created, so the safe response is to fall back to the local clock — submit
# at the appointed time without the timestamp — never to retry the same shape.
E_REMOTE_SCHEDULE = "remote_schedule_unsupported"

PERMANENT = frozenset({
    E_AUTH, E_ACCOUNT_AUTH, E_NOT_CONNECTED, E_VALIDATION, E_MEDIA_TOO_LARGE,
    E_MEDIA_UNFETCHABLE, E_UNSUPPORTED, E_DUPLICATE, E_PLAN_LIMIT,
})
TRANSIENT = frozenset({E_NETWORK, E_TIMEOUT, E_PROVIDER_5XX, E_MEDIA_PENDING})
CAPACITY = frozenset({E_RATE_LIMITED, E_QUOTA_EXHAUSTED})

# Errors that mean the DESTINATION is broken, not the post. These mark the
# destination unhealthy so the next 26 posts of the day don't each rediscover it.
DESTINATION_FATAL = frozenset({E_NOT_CONNECTED, E_ACCOUNT_AUTH})
# Errors that mean the CREDENTIAL is broken. Same reasoning, one level up.
# Deliberately narrow: everything in here disables publishing for a whole group,
# so a failure only belongs when it is provably about the API key itself.
CREDENTIAL_FATAL = frozenset({E_AUTH})


class ProviderError(Exception):
    """A classified provider failure.

    ``retryable`` and ``defer_seconds`` are the only two things the dispatcher
    reads. Everything else is for the operator.
    """

    def __init__(self, code: str, message: str = "", *,
                 status_code: Optional[int] = None,
                 defer_seconds: Optional[int] = None,
                 response: Optional[dict] = None,
                 provider_post_ref: Optional[str] = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status_code = status_code
        self.defer_seconds = defer_seconds
        self.response = response or {}
        # Set when the provider accepted something before failing — without it,
        # an ambiguous failure has no handle for a human to check.
        self.provider_post_ref = provider_post_ref

    @property
    def retryable(self) -> bool:
        return self.code in TRANSIENT or self.code in CAPACITY

    @property
    def is_capacity(self) -> bool:
        return self.code in CAPACITY

    @property
    def is_ambiguous(self) -> bool:
        """True when the post may or may not have gone live.

        Never auto-retried. See state.UNKNOWN.
        """
        return self.code == E_UNKNOWN

    def __repr__(self):
        return (f"ProviderError({self.code}, status={self.status_code}, "
                f"retryable={self.retryable})")


def is_retryable(code: str) -> bool:
    return code in TRANSIENT or code in CAPACITY


def classify_http_status(status: int, body: Optional[dict] = None) -> str:
    """Fallback mapping for an HTTP status with no adapter-specific handling.

    Adapters should classify from the body when they can — this is the floor,
    not the ceiling.
    """
    body = body or {}
    if status == 401:
        return E_AUTH
    if status == 402:
        return E_PLAN_LIMIT
    if status == 403:
        return E_NOT_CONNECTED
    if status == 404:
        return E_VALIDATION
    if status == 413:
        return E_MEDIA_TOO_LARGE
    if status == 422:
        return E_MEDIA_UNFETCHABLE
    if status == 429:
        return E_RATE_LIMITED
    if 400 <= status < 500:
        return E_VALIDATION
    if status >= 500:
        return E_PROVIDER_5XX
    return E_UNKNOWN
