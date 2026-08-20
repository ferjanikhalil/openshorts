"""Public publishing API — the endpoints the dashboard calls.

Three ideas shape this module.

**One publish endpoint, three modes.** ``POST /api/publishing/publish`` takes any
mix of ``destination_ids`` and ``group_ids``. Publishing one clip to one TikTok
account, to a hand-picked set spanning several groups, or to a whole group is the
same request with a different selection — ``mode`` is recorded for the UI and
never branches behaviour. Adding a fourth way to choose destinations means
touching ``service.expand_destinations`` and nothing else.

**Preview before commit.** ``/preview`` runs the identical expansion and the same
pre-flight checks the dispatcher will run, but writes nothing. The operator sees
exactly which accounts would receive the clip and why any were dropped, before a
single quota slot is spent.

**No secret has a route here.** Credentials are created and revoked through
``admin_api``; this module never reads a ciphertext column and returns no
credential material of any kind. The one signed thing it serves is a media token,
which grants read access to exactly one clip file and expires.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from . import (
    admin_auth, clips, db, media, planner, platforms as plat, providers,
    service, state, views, webhooks,
)
from .config import settings
from .models import (
    PublishAttempt, PublishDestination, PublishEvent, PublishGroup,
    PublishRequest,
)
from .schemas import PublishCreate, PublishJobIn, PublishPreview, RetryIn

router = APIRouter(prefix="/api/publishing", tags=["publishing"])
# Webhook ingestion lives in its own module (it is unauthenticated and
# signature-gated) but is mounted through this router so app.py has one seam.
router.include_router(webhooks.router)


def _now():
    return datetime.now(timezone.utc)


def _uuid_or_404(value, label: str = "id"):
    """Parse a path/query UUID, 404ing on a malformed one.

    Without this a typo'd id raises ValueError deep in SQLAlchemy and surfaces as
    a 500, which reads like a server bug rather than a bad URL.
    """
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, f"{label} not found")


async def _viewer(request: Request):
    """(user_id, actor_label) for the caller.

    In cloud mode a JWT identity scopes what the caller can see and is recorded
    as the actor on every write. In self-host mode there is no auth anywhere in
    the app, so the caller is unscoped and labelled ``local`` — publishing does
    not invent an auth model the rest of the product does not have.
    """
    try:
        from cloud.auth import get_current_user_optional
        user = await get_current_user_optional(request)
    except Exception:
        user = None
    if user is not None:
        return user.id, str(getattr(user, "email", "") or user.id)
    ident = await admin_auth.optional_publishing_admin(request)
    if ident is not None:
        return ident.user_id, f"admin:{ident.label}"
    return None, "local"


def _scope(stmt, model, user_id):
    """Restrict a query to the caller's rows when there is an identity to scope
    by. With no identity (self-host) there is nothing to scope against."""
    if user_id is None:
        return stmt
    return stmt.where(model.user_id == user_id)


# --- Diagnostics ------------------------------------------------------------
@router.get("/health")
async def health():
    """Config sanity, for the admin UI banner and for a deploy smoke check.

    Reports what is misconfigured rather than only whether it is: an operator
    whose posts silently never go out needs to see "no public media origin", not
    ``{"ok": false}``.
    """
    warnings = list(media.reachability_warnings()) + admin_auth.config_warnings()
    return {
        "enabled": True,
        "dry_run": settings.dry_run,
        "default_provider": settings.default_provider,
        "providers": providers.describe(),
        "admin_api_mounted": admin_auth.admin_router_enabled(),
        # Which identity the operator should present. Not a secret — it tells the
        # admin UI whether to ask for a token or to rely on the logged-in JWT,
        # instead of showing "not reachable" with no next step.
        "admin_auth": {
            "token": admin_auth.token_auth_available(),
            "email": admin_auth.email_auth_available(),
        },
        "media_strategy": media.media_strategy(),
        "clip_resolver_registered": clips.has_resolver(),
        # `publisher` means this instance holds the clock but no clip files, so a
        # missing resolver is the design and not a fault. Without the distinction
        # an always-on publisher reports unhealthy for its whole life and sends
        # the operator hunting a phantom.
        "role": settings.role,
        "warnings": warnings,
        "ok": not warnings and (clips.has_resolver()
                                or settings.role == "publisher"),
    }


@router.get("/destinations")
async def list_destinations(request: Request,
                            group_id: Optional[str] = None,
                            platform: Optional[str] = None,
                            session=Depends(db.get_db)):
    """Every publish target the caller can choose, with its group label.

    This is what the publish modal renders: a flat list of accounts, because the
    account is the unit of publication. Grouping in the UI is presentation.
    """
    user_id, _actor = await _viewer(request)
    stmt = select(PublishDestination, PublishGroup).join(
        PublishGroup, PublishGroup.id == PublishDestination.publish_group_id)
    stmt = _scope(stmt, PublishGroup, user_id)
    if group_id:
        stmt = stmt.where(PublishDestination.publish_group_id
                          == _uuid_or_404(group_id, "group"))
    if platform:
        stmt = stmt.where(PublishDestination.platform == plat.normalize(platform))
    rows = (await session.execute(stmt.order_by(
        PublishGroup.name, PublishDestination.platform))).all()

    out = []
    for dest, group in rows:
        item = views.destination_out(dest)
        item["group_name"] = group.name
        item["group_enabled"] = bool(group.enabled)
        out.append(item)
    return {"destinations": out, "count": len(out)}


# --- Pre-flight -------------------------------------------------------------
async def _preflight(session, dest: PublishDestination,
                     info: Optional[dict]) -> dict:
    """Would this destination accept this clip right now, and if not, why?

    Runs the same checks in the same order as ``dispatcher.dispatch_attempt``, so
    a preview that says "ready" is not a different opinion from the dispatcher's.
    The checks that would merely defer (cooldown, quota) are reported as warnings
    rather than blockers — the post is still legitimate, it just will not go out
    this minute.
    """
    blockers, warnings = [], []

    if not dest.enabled:
        blockers.append("destination is disabled")
    if dest.health in ("blocked", "disconnected"):
        blockers.append(f"destination is {dest.health}"
                        + (f": {dest.health_detail}" if dest.health_detail else ""))

    if info is None or not info.get("filename"):
        blockers.append("the clip file is not available")
    else:
        reason = plat.check_video(dest.platform, info.get("size_bytes"),
                                 info.get("duration") or info.get("duration_seconds"))
        if reason:
            blockers.append(reason)

    cred = await service.active_credential(session, dest.publish_group_id)
    if cred is None:
        # Still a blocker either way — but "add one" is wrong advice when a key
        # is already there and the provider rejected it, so name the real state.
        rejected = await service.active_credential(
            session, dest.publish_group_id, include_invalid=True)
        if rejected is not None:
            detail = (f": {rejected.invalid_reason[:200]}"
                      if rejected.invalid_reason else "")
            blockers.append("the provider rejected this group's API key — "
                            "re-check or replace it in publishing settings"
                            + detail)
        else:
            blockers.append("this group has no usable API key — add one in "
                            "publishing settings")

    if dest.cooldown_until and dest.cooldown_until > _now():
        warnings.append(f"provider cooldown until {dest.cooldown_until.isoformat()}")
    if (dest.quota_remaining is not None and dest.quota_remaining <= 0
            and dest.quota_reset_at and dest.quota_reset_at > _now()):
        warnings.append(
            f"daily quota exhausted; resets {dest.quota_reset_at.isoformat()}")
    if dest.health == "unverified":
        warnings.append("never published to this account before; the first "
                        "successful post verifies it")

    return {"destination": views.destination_out(dest),
            "ready": not blockers, "blockers": blockers, "warnings": warnings}


@router.post("/preview")
async def preview(body: PublishPreview, request: Request,
                  session=Depends(db.get_db)):
    """Expand a selection WITHOUT creating anything.

    The operator sees the exact account list a publish would hit. Cheap enough to
    call on every checkbox change, and it is the only way to learn that a
    destination is out of quota before spending an attempt discovering it.
    """
    await _viewer(request)
    dests = await service.expand_destinations(
        session, destination_ids=body.destination_ids,
        group_ids=body.group_ids, platforms=body.platforms)

    info = None
    if body.job_id is not None and body.clip_index is not None:
        info = clips.resolve(body.job_id, body.clip_index)

    checks = [await _preflight(session, d, info) for d in dests]
    return {
        "destinations": checks,
        "count": len(checks),
        "ready_count": sum(1 for c in checks if c["ready"]),
        "platforms": sorted({d.platform for d in dests}),
        "clip": {
            "resolved": info is not None,
            "size_bytes": (info or {}).get("size_bytes"),
            "duration": (info or {}).get("duration"),
            "fingerprint": (info or {}).get("fingerprint"),
        } if (body.job_id is not None) else None,
        "media_ready": media.media_strategy() != "none",
    }


# --- Publish ----------------------------------------------------------------
def _mode_for(body: PublishCreate, destinations) -> str:
    """Label the request's provenance for the UI. Never gates behaviour."""
    if body.scheduled_for:
        return "scheduled"
    if body.group_ids and not body.destination_ids:
        return "group"
    if len(destinations) == 1:
        return "single"
    return "multi"


@router.post("/publish", status_code=201)
async def publish(body: PublishCreate, request: Request,
                  session=Depends(db.get_db)):
    """Publish one clip to any combination of accounts and groups.

    Returns 201 with the request and its per-destination attempts. The response
    is deliberately the full fan-out rather than an id: the operator asked for 9
    posts and needs to see 9 rows, including any destination that was dropped
    during expansion.

    Re-posting the same selection returns the ORIGINAL request (200-style body,
    ``duplicate: true``) instead of creating a second fan-out — the idempotency
    key is a UNIQUE column, so a double-clicked button cannot double-post even if
    both requests race.
    """
    user_id, actor = await _viewer(request)

    if not body.destination_ids and not body.group_ids:
        raise HTTPException(400, "select at least one destination or group")

    info = clips.resolve(body.job_id, body.clip_index)
    if info is None or not info.get("filename"):
        # Refused up front rather than queued: without bytes there is nothing to
        # publish, and a queued attempt would just fail 9 times.
        raise HTTPException(
            404, f"clip {body.clip_index} of job {body.job_id} is not available "
                 "(still processing, or removed by job retention)")

    dests = await service.expand_destinations(
        session, destination_ids=body.destination_ids,
        group_ids=body.group_ids, platforms=body.platforms)
    if not dests:
        raise HTTPException(
            400, "that selection expands to no usable destination — every "
                 "matching account is disabled, blocked or disconnected")

    payload = {
        "title": body.title,
        "caption": body.caption,
        "per_platform": body.per_platform or {},
        # Frozen here so editing a caption template later never rewrites what a
        # live post says.
        "resolved_at": _now().isoformat(),
    }

    existing_key = body.idempotency_key or state.derive_idempotency_key(
        body.job_id, body.clip_index, [str(d.id) for d in dests],
        body.scheduled_for.isoformat() if body.scheduled_for else None)
    prior = (await session.execute(select(PublishRequest).where(
        PublishRequest.idempotency_key == existing_key))).scalar_one_or_none()

    req = await service.create_request(
        session, job_id=body.job_id, clip_index=body.clip_index,
        destinations=dests, payload=payload,
        mode=_mode_for(body, dests), scheduled_for=body.scheduled_for,
        content_fingerprint=info.get("fingerprint"),
        idempotency_key=existing_key, user_id=user_id, actor=actor)
    await session.commit()

    attempts = (await session.execute(
        select(PublishAttempt).where(
            PublishAttempt.publish_request_id == req.id)
        .order_by(PublishAttempt.platform))).scalars().all()
    by_id = {d.id: d for d in dests}
    return {
        **views.request_out(req, [views.attempt_out(a, by_id.get(
            a.publish_destination_id)) for a in attempts]),
        "duplicate": prior is not None,
        "dispatch": "immediate" if not body.scheduled_for else "scheduled",
    }


@router.post("/publish-job", status_code=201)
async def publish_job(body: PublishJobIn, request: Request,
                      session=Depends(db.get_db)):
    """Schedule several of a job's clips in one call, spaced automatically.

    Distinct from ``POST /publish`` in exactly one way: it creates one request
    per clip and spreads them, instead of one request now. The spread is the
    reason it exists — firing a job's clips at one account simultaneously earns a
    429 on everything after the first, then hours of backoff.
    """
    user_id, actor = await _viewer(request)
    if not body.destination_ids and not body.group_ids:
        raise HTTPException(400, "select at least one destination or group")

    report = await planner.plan_job(
        session, job_id=body.job_id, clip_count=body.clip_count,
        plan=body.model_dump(exclude_none=True) | {"source": "dashboard"},
        user_id=user_id, actor=actor)
    if not report.get("ok"):
        raise HTTPException(400, report.get("reason") or "nothing to publish")
    # All clips were reused: no new request, no new attempt, nothing for the
    # dispatcher to pick up. That is not a 404 (the clips exist) and not a 201
    # (nothing was queued) — it is an informational 200 so the dashboard can say
    # "this was already published" instead of "done, go look".
    if not report.get("created") and not report.get("existing"):
        raise HTTPException(
            404, "none of those clips are available (still processing, or "
                 "removed by job retention)")
    await session.commit()
    if not report.get("created") and report.get("existing"):
        request.status_code = 200
    return report


# --- Reading publication history --------------------------------------------
@router.get("/requests")
async def list_requests(request: Request,
                        job_id: Optional[str] = None,
                        status: Optional[str] = None,
                        needs_attention: bool = False,
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        session=Depends(db.get_db)):
    """Publication history, newest first.

    ``needs_attention=true`` is the operator's real query: which posts are in a
    state only a human can resolve (``unknown``, ``dead``, ``blocked``). It filters
    on the attempts, not the request status, because a 3-account request that is
    ``partial`` may have exactly one row that needs looking at.
    """
    user_id, _actor = await _viewer(request)
    stmt = _scope(select(PublishRequest), PublishRequest, user_id)
    if job_id:
        stmt = stmt.where(PublishRequest.job_id == job_id)
    if status:
        stmt = stmt.where(PublishRequest.status == status)
    if needs_attention:
        stmt = stmt.where(PublishRequest.id.in_(
            select(PublishAttempt.publish_request_id).where(
                PublishAttempt.status.in_(list(state.NEEDS_ATTENTION)))))
    rows = (await session.execute(
        stmt.order_by(PublishRequest.created_at.desc())
        .limit(limit).offset(offset))).scalars().all()

    out = []
    for req in rows:
        attempts = (await session.execute(
            select(PublishAttempt, PublishDestination)
            .join(PublishDestination,
                  PublishDestination.id == PublishAttempt.publish_destination_id)
            .where(PublishAttempt.publish_request_id == req.id)
            .order_by(PublishAttempt.created_at))).all()
        out.append(views.request_out(
            req, [views.attempt_out(a, d) for a, d in attempts]))
    return {"requests": out, "count": len(out),
            "limit": limit, "offset": offset}


async def _load_request(session, request_id, user_id):
    req = await session.get(PublishRequest,
                            _uuid_or_404(request_id, "publish request"))
    if req is None:
        raise HTTPException(404, "publish request not found")
    if user_id is not None and req.user_id not in (None, user_id):
        # 404 rather than 403: existence itself is not the caller's business.
        raise HTTPException(404, "publish request not found")
    return req


@router.get("/requests/{request_id}")
async def get_request(request_id: str, request: Request,
                      session=Depends(db.get_db)):
    """One request with EVERY attempt, including superseded retries.

    History is append-only, so the full list answers "how many times did we try
    TikTok 2, and what did it say each time" — which is the question that actually
    comes up when a post is missing.
    """
    user_id, _actor = await _viewer(request)
    req = await _load_request(session, request_id, user_id)
    attempts = (await session.execute(
        select(PublishAttempt, PublishDestination)
        .join(PublishDestination,
              PublishDestination.id == PublishAttempt.publish_destination_id)
        .where(PublishAttempt.publish_request_id == req.id)
        .order_by(PublishAttempt.created_at))).all()
    events = (await session.execute(
        select(PublishEvent).where(PublishEvent.publish_request_id == req.id)
        .order_by(PublishEvent.created_at))).scalars().all()
    return {
        **views.request_out(req,
                            [views.attempt_out(a, d) for a, d in attempts]),
        "events": [views.event_out(e) for e in events],
    }


@router.post("/requests/{request_id}/cancel")
async def cancel(request_id: str, request: Request,
                 session=Depends(db.get_db)):
    """Cancel what has not reached the provider yet.

    Necessarily partial, and says so: an already-submitted post cannot be
    recalled (the provider exposes no working cancel endpoint), so the response
    reports what was stopped and what was already gone rather than implying a
    clean rollback.
    """
    user_id, actor = await _viewer(request)
    req = await _load_request(session, request_id, user_id)
    result = await service.cancel_request(session, req.id, actor=actor)
    await session.commit()
    return result


@router.post("/attempts/{attempt_id}/retry")
async def retry(attempt_id: str, body: RetryIn, request: Request,
                session=Depends(db.get_db)):
    """Retry one destination's attempt. The destination, never the whole request.

    Retrying an ``unknown`` attempt requires ``force: true``. That post may
    already be live, so the system will not make that call on its own — and the
    409 body says exactly that instead of a generic conflict.
    """
    user_id, actor = await _viewer(request)
    attempt = await session.get(PublishAttempt,
                                _uuid_or_404(attempt_id, "attempt"))
    if attempt is None:
        raise HTTPException(404, "attempt not found")
    await _load_request(session, attempt.publish_request_id, user_id)

    result = await service.retry_attempt(session, attempt.id, actor=actor,
                                        force=body.force)
    if not result.get("ok"):
        await session.rollback()
        raise HTTPException(409, result.get("reason", "cannot retry"))
    await session.commit()
    return result


@router.get("/attempts")
async def list_attempts(request: Request,
                        status: Optional[str] = None,
                        platform: Optional[str] = None,
                        group_id: Optional[str] = None,
                        needs_attention: bool = False,
                        limit: int = Query(100, ge=1, le=500),
                        session=Depends(db.get_db)):
    """The per-account status board.

    Queryable without joining through a group, which is the point of putting
    ``publish_group_id`` and ``platform`` on the attempt row: "what did TikTok 2
    do today" is one indexed scan.
    """
    await _viewer(request)
    stmt = (select(PublishAttempt, PublishDestination)
            .join(PublishDestination,
                  PublishDestination.id == PublishAttempt.publish_destination_id))
    if status:
        stmt = stmt.where(PublishAttempt.status == status)
    if needs_attention:
        stmt = stmt.where(PublishAttempt.status.in_(list(state.NEEDS_ATTENTION)))
    if platform:
        stmt = stmt.where(PublishAttempt.platform == plat.normalize(platform))
    if group_id:
        stmt = stmt.where(PublishAttempt.publish_group_id
                          == _uuid_or_404(group_id, "group"))
    rows = (await session.execute(
        stmt.order_by(PublishAttempt.created_at.desc()).limit(limit))).all()
    return {"attempts": [views.attempt_out(a, d) for a, d in rows],
            "count": len(rows)}


# --- Signed media -----------------------------------------------------------
# HEAD as well as GET: downloaders probe with HEAD before fetching (Status 200's
# does), and FastAPI does NOT add HEAD to a @router.get route — so the probe got
# a 405 and the fetch looked broken before it started. FileResponse handles the
# bodyless case itself.
@router.api_route("/media/{token}", methods=["GET", "HEAD"])
async def serve_media(token: str):
    """Serve ONE clip to the provider, gated by an HMAC token.

    This exists because Status 200 fetches media by URL: it needs a public link,
    and the output directory must not become one. The token pins job, clip index,
    filename and an expiry, so it grants exactly one file for a bounded time and
    cannot be edited into a path traversal — a changed filename changes the MAC.

    The slow route by construction: the provider downloads the whole clip from
    here, inside its own submit request. Deploys with an object store configured
    (see publishing/objectstore.py) never reach it, and neither do cloud deploys,
    which prefer R2 presigned URLs.
    """
    ok, payload, reason = media.verify_media_request(token)
    if not ok:
        # No detail about which check failed, and 404 rather than 403: this URL is
        # handed to a third party, so it should reveal nothing about what a valid
        # one would look like.
        print(f"⚠️  Publishing: rejected media token ({reason})")
        raise HTTPException(404, "not found")

    job_id = payload.get("j")
    clip_index = payload.get("c")
    filename = payload.get("f")

    info = clips.resolve(job_id, clip_index)
    if info is None or not info.get("output_dir"):
        raise HTTPException(404, "not found")

    path = media.clip_local_path(info["output_dir"], job_id, filename)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "not found")

    return FileResponse(
        path, media_type="video/mp4", filename=filename,
        # The provider fetches once; caching a URL that expires would only serve
        # a stale body after the token dies.
        headers={"Cache-Control": "no-store"})
