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
    # One request carries one platform (vs. a multi-platform fan-out per call).
    one_platform_per_request: bool = True


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
