"""Who is allowed to configure publishing.

The repo has no RBAC: there is no ``is_admin`` column, no role table, and with
``BILLING_ENABLED`` off there is no authentication at all. The admin surface here
holds provider API keys, so shipping it behind "whoever can reach port 8000"
would be a credential-disclosure bug.

Two identities, in priority order:

  cloud mode      the JWT identity must appear in ``PUBLISHING_ADMIN_EMAILS``.
  self-host       an ``X-Publishing-Admin-Token`` matching
                  ``PUBLISHING_ADMIN_TOKEN``, compared in constant time.

If NEITHER is configured, ``admin_router_enabled()`` is False and the admin
router is never mounted (see ``publishing/__init__.py``). That is the deliberate
failure mode: publishing goes inert rather than open. There is no "no admin
configured means everyone is admin" path anywhere in this module.
"""
import hmac
from typing import Optional

from fastapi import HTTPException, Request

from .config import settings

ADMIN_TOKEN_HEADER = "X-Publishing-Admin-Token"
# A short token is a guessable token. This is a service credential the operator
# generates, not a password they type, so demanding real entropy costs nothing.
MIN_TOKEN_LENGTH = 24


def token_auth_available() -> bool:
    tok = settings.admin_token
    return bool(tok) and len(tok) >= MIN_TOKEN_LENGTH


def email_auth_available() -> bool:
    return bool(settings.admin_emails)


def admin_router_enabled() -> bool:
    """True when at least one admin identity mechanism is usable."""
    return token_auth_available() or email_auth_available()


def config_warnings() -> list:
    """Boot-time diagnostics — surfaced in logs and on the admin health endpoint."""
    out = []
    tok = settings.admin_token
    if tok and len(tok) < MIN_TOKEN_LENGTH:
        out.append(
            f"PUBLISHING_ADMIN_TOKEN is only {len(tok)} chars; it is ignored. "
            f"Use at least {MIN_TOKEN_LENGTH} random chars: "
            "python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    if not admin_router_enabled():
        out.append(
            "No publishing admin identity configured — the admin API is not "
            "mounted, so no credential can be added and nothing will publish. "
            "Set PUBLISHING_ADMIN_TOKEN (self-host) or PUBLISHING_ADMIN_EMAILS "
            "(cloud)."
        )
    return out


class AdminIdentity:
    """Who performed an admin action. Recorded in the publish_events audit log."""

    __slots__ = ("kind", "label", "user_id")

    def __init__(self, kind: str, label: str, user_id=None):
        self.kind = kind          # 'token' | 'email'
        self.label = label        # audit string, e.g. an email or 'admin-token'
        self.user_id = user_id    # cloud user UUID when known, else None

    def __repr__(self):
        return f"AdminIdentity({self.kind}:{self.label})"


def _check_token(request: Request) -> Optional[AdminIdentity]:
    if not token_auth_available():
        return None
    presented = request.headers.get(ADMIN_TOKEN_HEADER, "")
    if not presented:
        return None
    # Constant-time: a naive == leaks the prefix length through timing.
    if hmac.compare_digest(presented, settings.admin_token):
        return AdminIdentity("token", "admin-token")
    return None


async def _check_email(request: Request) -> Optional[AdminIdentity]:
    if not email_auth_available():
        return None
    try:
        from cloud.auth import get_current_user_optional
    except Exception:
        # cloud/ is importable but its deps (JWT, DB) may not be configured.
        return None
    try:
        user = await get_current_user_optional(request)
    except Exception:
        return None
    if user is None or not user.email:
        return None
    if str(user.email).lower() in settings.admin_emails:
        return AdminIdentity("email", str(user.email).lower(), user_id=user.id)
    return None


async def require_publishing_admin(request: Request) -> AdminIdentity:
    """FastAPI dependency. 401s anything that is not a configured admin."""
    ident = _check_token(request)
    if ident is not None:
        return ident
    ident = await _check_email(request)
    if ident is not None:
        return ident
    # One undifferentiated message: distinguishing "wrong token" from "not an
    # admin" would confirm which mechanism is live to an unauthenticated caller.
    raise HTTPException(
        status_code=401,
        detail="Publishing admin credentials required.",
        headers={"WWW-Authenticate": ADMIN_TOKEN_HEADER},
    )


async def optional_publishing_admin(request: Request) -> Optional[AdminIdentity]:
    """Non-raising variant, for endpoints that widen their view for admins."""
    ident = _check_token(request)
    if ident is not None:
        return ident
    return await _check_email(request)
