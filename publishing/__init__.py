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

There are two other entrypoints, both for a host that holds the schedule clock
because this one may be asleep at a slot (see ``deploy/publisher/README.md``):
``runner.py`` for an always-on process, ``tick.py`` for a scheduled one.
"""
import sys

from .config import is_enabled, settings, validate_required  # noqa: F401


def make_stdio_utf8_safe() -> None:
    """Stop a log character from aborting the boot.

    Publishing's boot lines carry emoji, and a Windows console defaults to
    cp1252, which cannot encode them: ``print`` raises UnicodeEncodeError and the
    process dies before the first loop starts. Inside a Linux container this
    never happens, but the obvious way to smoke-test either entrypoint before
    deploying anything is to run it on a laptop — and dying there with a codec
    error, on the very first warning, is a terrible first impression of a
    component whose whole job is to be dependable.

    Called by the entrypoints, not at import: ``app.py`` owns its own stdio.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - not a text stream
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - already redirected somewhere odd
            pass


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
    # The media strategy belongs in the boot line: it decides whether the
    # provider's download crosses this machine's uplink, which is the difference
    # between a post going out and a submit timing out into `unknown`.
    print("📡 Publishing mode ENABLED (DB ready, dispatch + reconcile + "
          f"transfer active, media: {media.media_strategy()}).")
