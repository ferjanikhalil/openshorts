"""The provider contract.

Everything outside this package speaks only in the types defined here. That is
the whole point: no ``if provider == "status200"`` exists in the dispatcher, the
API, the workers or the UI, so adding a second provider is a new file in this
directory plus a registry entry.

Precedent for the shape: ``batch.OPERATIONS`` — "adding a new operation requires
only registering it in OPERATIONS, no orchestrator changes."

``Capabilities`` is the mechanism that keeps the abstraction honest. Providers
differ in ways that change orchestration, not just wire format — Status 200 has
no verified status-lookup endpoint and no working cancel-scheduled endpoint. A
generic layer cannot paper over that, so instead the provider declares it and the
orchestrator adapts: no polling loop when lookup is unsupported, local scheduling
when remote cancel is unsupported.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually do. Declared, not assumed."""

    name: str
    platforms: tuple = ()

    # --- Operator-facing identity ---------------------------------------------
    # The admin UI renders these instead of hardcoding a provider name. It had
    # six such strings ("status 200 api key", "rl_…", "Paste the secret Status
    # 200 generated…") and every one of them lied the moment a second provider
    # existed — a Zernio batch told the operator to paste a Status 200 key.
    # `label` is the display name; empty means "use `name`".
    label: str = ""
    # Leading characters of this provider's API keys, used as the input's
    # placeholder. Not validated against — a provider may change its prefix and
    # a wrong placeholder must not refuse a working key.
    key_prefix: str = ""
    # True for an adapter that publishes nowhere — the dry-run fake. Declared
    # rather than name-checked so the admin UI can leave it out of the "which
    # provider?" picker without the frontend knowing which adapter is the fake.
    # Creating a group on a simulated provider would look normal and silently
    # publish nothing.
    simulated: bool = False

    # Media is uploaded once and the returned ref is reusable across posts and
    # platforms. False means upload per submission.
    supports_media_refs: bool = False
    # Media is ingested from a public URL we provide (vs. a byte upload).
    media_by_url: bool = False
    media_ref_ttl_seconds: Optional[int] = None

    # A GET that returns the current state of a submitted post. When False there
    # is NO polling fallback and webhooks are the only completion signal — the
    # stale-attempt sweeper becomes the safety net.
    supports_status_lookup: bool = False
    # Remote scheduling. When False, scheduling is held locally and the submit
    # happens at the appointed time.
    supports_remote_schedule: bool = False
    # Cancelling an already-scheduled remote post. When False, never schedule
    # remotely — an uncancellable scheduled post is an uncontrollable one.
    supports_cancel_scheduled: bool = False
    # Enumerating the accounts connected to a credential. When False,
    # destinations are entered by an admin and proven by a canary post.
    supports_account_listing: bool = False
    # Signed inbound callbacks.
    supports_webhooks: bool = False
    # HTTP header the provider signs its callbacks with. Declared because every
    # provider picks its own name and the receiver must not hardcode one — Status
    # 200 sends X-Webhook-Signature, Zernio sends X-Zernio-Signature, and reading
    # the wrong header rejects every callback as unsigned.
    signature_header: str = "X-Webhook-Signature"
    # One request carries one platform (vs. a multi-platform fan-out per call).
    one_platform_per_request: bool = True
    # Several independent provider accounts may live in ONE publishing group,
    # addressed by credential_slot. True only where the provider's own account
    # model forces it: Zernio's free tier connects 2 social accounts per Zernio
    # account, so 3 destinations need 2 keys in one logical batch. Providers that
    # leave this False reject a credential slot at the API instead of storing a
    # key nothing would ever resolve.
    multi_credential: bool = False


@dataclass
class MediaRef:
    """A provider-side handle for uploaded media."""

    ref: str
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    expires_at: Optional[datetime] = None


@dataclass
class PublishPayload:
    """What to post. Provider-neutral; the adapter maps it to its wire format."""

    platform: str
    provider_account_ref: str
    caption: str = ""
    title: str = ""
    media_ref: Optional[str] = None
    media_url: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    # Per-platform extras (privacy status, hashtags, tags). Passed through by the
    # adapter; the orchestrator never inspects it.
    options: dict = field(default_factory=dict)


@dataclass
class SubmitResult:
    """Outcome of one submission to one destination."""

    # 'succeeded'  provider confirms the post is live
    # 'submitted'  accepted, outcome pending (webhook or sweeper resolves it).
    #              Also the right answer for a post the provider PARKED for a
    #              later window: it exists on their side, so re-sending it would
    #              publish a duplicate. `defer_seconds` then means "do not call
    #              this silence suspicious before then", not "send it again".
    # 'deferred'   provider created NOTHING and refused for capacity; the same
    #              payload must be submitted again after defer_seconds
    status: str
    provider_post_ref: Optional[str] = None
    provider_native_post_ref: Optional[str] = None
    permalink: Optional[str] = None
    defer_seconds: Optional[int] = None
    # Parsed quota view for the destination row: limit / remaining / reset_at.
    quota: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    # Set when a submit asked the provider to hold a FUTURE slot and the response
    # shows the post already live. The post is real (so this is 'succeeded', not
    # a failure) but the schedule was silently dropped: the orchestrator logs it
    # and stops handing the clock over. Without this, an accept-then-ignore is
    # indistinguishable from a post genuinely parked until its slot.
    schedule_ignored: bool = False


@dataclass
class WebhookEvent:
    """A normalized inbound provider callback."""

    event_id: str
    # 'post.published' | 'post.failed' | 'post.scheduled'
    # | 'account.disconnected' | 'unknown'
    event_type: str
    provider_post_ref: Optional[str] = None
    provider_native_post_ref: Optional[str] = None
    provider_account_ref: Optional[str] = None
    permalink: Optional[str] = None
    error_message: Optional[str] = None
    # Adapter's reading of a 'post.failed' reason, as an errors.py code. None
    # means "no opinion, treat as a retryable provider failure". Providers that
    # report "we stopped waiting" as a failure must set errors.E_UNKNOWN here:
    # the post may be live, and a retry on a live post double-publishes.
    error_code: Optional[str] = None
    created_at: Optional[float] = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """The five things a provider must do.

    Adapters raise ``errors.ProviderError`` with a classified code — never a bare
    httpx exception — so the dispatcher's retry decision never depends on which
    provider produced the failure.
    """

    capabilities: Capabilities

    async def upload_media(self, api_key: str, *, media_url: str,
                           mime_type: Optional[str] = None) -> MediaRef:
        """Register media with the provider and return a reusable ref."""
        ...

    async def submit(self, api_key: str, payload: PublishPayload) -> SubmitResult:
        """Submit one post to one destination."""
        ...

    async def fetch_status(self, api_key: str,
                           provider_post_ref: str) -> Optional[SubmitResult]:
        """Current state of a submitted post, or None when unsupported."""
        ...

    def parse_webhook(self, payload: dict) -> WebhookEvent:
        """Normalize an inbound callback body."""
        ...

    async def verify_destination(self, api_key: str, platform: str,
                                 provider_account_ref: str) -> dict:
        """Best-effort reachability check for one destination.

        Returns ``{"health": "ok"|"blocked"|"unverified", "detail": str}``.
        Must NOT create a real post.
        """
        ...


# --- Optional adapter hooks -------------------------------------------------
# Not part of the Protocol, because every one of them has a correct default and a
# provider that does not need it should not have to write a stub. The caller
# reaches them with getattr(provider, name, None), so an adapter opts in.
#
#   verify_signature(secret, raw_body, presented) -> bool
#       Override webhook signature verification. Default:
#       ``signing.verify_webhook_signature`` (hex, ``sha256=`` prefix optional).
#       Override when the provider's encoding differs or is undocumented.
#   remote_schedule_ok() -> bool
#   disable_remote_schedule(reason) -> None
#       Process-lifetime health of the remote-schedule field, so one
#       accepted-and-ignored timestamp stops the hand-over instead of a day's
#       worth of posts firing at once.
#   check_credential(api_key) -> {"ok", "code", "detail"}
#       Non-destructive "is this key accepted at all". Must not create a post.
#   list_accounts(api_key) -> list[dict]
#       Only where ``supports_account_listing`` is True.
#   cancel(api_key, provider_post_ref) -> bool
#       Only where ``supports_cancel_scheduled`` is True.
