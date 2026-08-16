"""Automated social-media publishing for OpenShorts.

This package is dormant unless ``PUBLISHING_ENABLED`` is set. ``app.py`` imports
it only in that case, so self-hosters who don't publish never pay for the extra
dependencies or the DB requirement.

Domain vocabulary (deliberately provider-neutral — see providers/base.py):

  publish_group        A reusable grouping of destinations sharing ONE provider
                       credential. The UI calls this a "Batch".
  publish_destination  ONE specific connected social account (platform +
                       provider_account_ref). This is the atomic publish target.
  publish_request      One user-visible publish operation for one video.
  publish_attempt      ONE row per destination per try. THE publication record.

The destination — not the group — is the unit of publication. A group only ever
*expands* into destinations, which is what lets the same machinery serve
single-account, hand-picked multi-account, and whole-batch publishing without
three code paths.

Wiring mirrors ``cloud/`` (Starlette forbids adding middleware after start):
  - ``setup_sync(app)``  -> import time (routers)
  - ``setup_async(app)`` -> lifespan (DB engine, background loops)
"""
from .config import is_enabled, settings, validate_required  # noqa: F401


def setup_sync(app):
    """Attach routers at import time. Fails fast on missing/invalid config."""
    validate_required()

    from . import api
    app.include_router(api.router)
    # The admin router is mounted ONLY when an admin identity can be enforced.
    # In self-host mode there is no auth at all, so without an admin token the
    # credential endpoints would be world-readable on port 8000. Staying
    # unmounted is the safe failure: publishing is inert, not open.
    from . import admin_auth
    if admin_auth.admin_router_enabled():
        from . import admin_api
        app.include_router(admin_api.router)
        print("📡 Publishing admin API mounted.")
    for warning in admin_auth.config_warnings():
        print(f"⚠️  {warning}")


async def setup_async(app):
    """DB engine + background loops. Runs inside the FastAPI lifespan."""
    from . import db, media, worker

    await db.init()
    for warning in media.reachability_warnings():
        print(f"⚠️  {warning}")
    await worker.recover_stale_on_boot()
    worker.start_loops()
    print("📡 Publishing mode ENABLED (DB ready, dispatcher + reconciler active).")
