"""Provider registry.

``get(name)`` is the only way the rest of the application reaches a provider, so
the set of adapters is a data structure rather than a set of import sites.
Mirrors ``batch.OPERATIONS``.
"""
from typing import Dict

from .base import (  # noqa: F401 — re-exported as the public contract
    Capabilities, MediaRef, Provider, PublishPayload, SubmitResult, WebhookEvent,
)

_REGISTRY: Dict[str, object] = {}


def register(name: str, provider) -> None:
    _REGISTRY[name] = provider


def get(name: str):
    """Return the adapter for ``name``.

    In dry-run mode every lookup resolves to the fake adapter, so the entire
    pipeline — queue, retries, webhooks, UI — runs end to end with no live
    credential and no real post. That is what makes the system testable before
    the operator has provisioned anything.
    """
    from ..config import settings
    if settings.dry_run:
        return _REGISTRY["fake"]
    provider = _REGISTRY.get(name)
    if provider is None:
        raise KeyError(
            f"unknown publishing provider '{name}'. Registered: "
            f"{', '.join(sorted(_REGISTRY)) or 'none'}"
        )
    return provider


def names() -> list:
    return sorted(_REGISTRY)


def describe() -> list:
    """Provider list for the admin UI: name + declared capabilities.

    The UI reads these instead of hardcoding provider names — that is what let a
    second provider ship without touching the forms. ``multi_credential`` in
    particular decides whether the credential slot field is offered at all.
    """
    out = []
    for name in names():
        caps = getattr(_REGISTRY[name], "capabilities", None)
        out.append({
            "name": name,
            # Display name and key placeholder, so no form has to know a provider
            # by name to label itself correctly.
            "label": getattr(caps, "label", "") or name,
            "key_prefix": getattr(caps, "key_prefix", "") or "",
            # Publishes nowhere. The UI leaves these out of the provider picker.
            "simulated": bool(getattr(caps, "simulated", False)),
            "platforms": list(getattr(caps, "platforms", ()) or ()),
            "supports_account_listing": bool(
                getattr(caps, "supports_account_listing", False)),
            "supports_remote_schedule": bool(
                getattr(caps, "supports_remote_schedule", False)),
            "supports_cancel_scheduled": bool(
                getattr(caps, "supports_cancel_scheduled", False)),
            "supports_webhooks": bool(getattr(caps, "supports_webhooks", False)),
            "supports_status_lookup": bool(
                getattr(caps, "supports_status_lookup", False)),
            "multi_credential": bool(getattr(caps, "multi_credential", False)),
        })
    return out


# Import adapters for their side-effecting registration. Kept at the bottom so
# an adapter can import the registry's own types without a cycle.
from . import status200  # noqa: E402,F401
from . import zernio     # noqa: E402,F401
from . import fake       # noqa: E402,F401
