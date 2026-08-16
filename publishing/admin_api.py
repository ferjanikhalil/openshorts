"""Admin API — groups, credentials, destinations.

This is the "secure admin interface" half of the design: the operator configures
publishing here, at runtime, and no API key is ever written to source, ``.env``,
Git, frontend code or a log line.

Three rules this module exists to enforce:

**A secret goes in and never comes out.** ``PUT .../credential`` is the only
endpoint on the whole surface that accepts a provider key. Nothing here returns
one — the responses are built by ``views.credential_out``, which enumerates its
fields, so a future column cannot ride along into JSON. The readable identity of a
key is its fingerprint and last 4 characters, which is enough to answer "is this
the key I pasted?" and nothing more.

**Rotation is by-insert.** Setting a new key revokes the old row rather than
overwriting it, so a historical attempt can still say which credential signed it.

**The router is not mounted unless an admin identity can be enforced** — see
``publishing/__init__.py``. There is no "no admin configured" fallback here.
"""
from datetime import datetime, timezone
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from . import (
    admin_auth, crypto, db, media, planner, platforms as plat, providers,
    schedule, service, state, views,
)
from .admin_auth import AdminIdentity, require_publishing_admin
from .config import settings
from .models import (
    PublishAssignment, PublishAttempt, PublishCredential, PublishDestination,
    PublishEvent, PublishGroup,
)
from .schemas import (
    AssignmentBulkCreate, CredentialCreate, DestinationCreate,
    DestinationUpdate, GroupCreate, GroupUpdate,
)

# Router-level dependency, not per-handler: a new endpoint added below is
# authenticated by default instead of by remembering to say so.
router = APIRouter(prefix="/api/publishing/admin", tags=["publishing-admin"],
                   dependencies=[Depends(require_publishing_admin)])


def _now():
    return datetime.now(timezone.utc)


def _uuid_or_404(value, label: str):
    """Parse a path UUID, 404ing on a malformed one rather than 500ing."""
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, f"{label} not found")


def _credential_aad(group_id, kind: str, provider: str) -> str:
    """Bind a ciphertext to its group, kind and provider.

    GCM verifies the AAD on decrypt, so a webhook secret blob copied into an
    api_key row — or a credential moved between groups in the database — fails to
    decrypt instead of silently authenticating as something it is not.
    """
    return f"group:{group_id}|kind:{kind}|provider:{provider}"


async def _get_group(session, group_id) -> PublishGroup:
    group = await session.get(PublishGroup,
                              _uuid_or_404(group_id, "publish group"))
    if group is None:
        raise HTTPException(404, "publish group not found")
    return group


def _webhook_url(provider: str) -> Optional[str]:
    """The callback URL to paste into the provider's dashboard.

    None when no public origin is configured, which is the honest answer: the
    provider has to reach it from the public internet, so there is no usable URL
    to show.
    """
    base = settings.public_base_url
    return f"{base}/api/publishing/webhook/{provider}" if base else None


async def _group_payload(session, group: PublishGroup) -> dict:
    cred = await service.active_credential(session, group.id)
    hook = await service.active_credential(session, group.id,
                                           kind="webhook_secret")
    dests = (await session.execute(
        select(PublishDestination)
        .where(PublishDestination.publish_group_id == group.id)
        .order_by(PublishDestination.platform,
                  PublishDestination.display_name))).scalars().all()
    return views.group_out(
        group,
        credential=views.credential_out(cred) if cred else None,
        webhook_secret=views.credential_out(hook) if hook else None,
        webhook_url=_webhook_url(group.provider),
        destinations=[views.destination_out(d) for d in dests],
        summary=await service.group_summary(session, group.id))


# --- Diagnostics ------------------------------------------------------------
@router.get("/health")
async def admin_health(ident: AdminIdentity = Depends(require_publishing_admin)):
    """Everything an operator needs to see why publishing is or is not working."""
    return {
        "admin": {"kind": ident.kind, "label": ident.label},
        "dry_run": settings.dry_run,
        "default_provider": settings.default_provider,
        "providers": providers.describe(),
        "platforms": list(plat.KNOWN_PLATFORMS),
        "max_attempts": settings.max_attempts,
        "media_strategy": "r2_presigned" if media.r2_available()
                          else ("signed_token" if settings.public_base_url
                                else "none"),
        "warnings": list(media.reachability_warnings())
                    + admin_auth.config_warnings(),
    }


# --- Groups -----------------------------------------------------------------
@router.get("/groups")
async def list_groups(session=Depends(db.get_db)):
    """Every group with its masked credential, destinations and counts."""
    groups = (await session.execute(
        select(PublishGroup).order_by(PublishGroup.created_at))).scalars().all()
    return {"groups": [await _group_payload(session, g) for g in groups],
            "count": len(groups)}


@router.post("/groups", status_code=201)
async def create_group(body: GroupCreate,
                       ident: AdminIdentity = Depends(require_publishing_admin),
                       session=Depends(db.get_db)):
    """Create a group. Its credential and destinations are added separately.

    Empty on purpose: a group with no key and no accounts is inert, so the
    operator can shape the structure first and paste secrets once, rather than
    being asked for a key mid-form.
    """
    provider = body.provider or settings.default_provider
    try:
        providers.get(provider)
    except KeyError as e:
        raise HTTPException(400, str(e))

    group = PublishGroup(
        name=body.name.strip(), provider=provider, enabled=body.enabled,
        settings=body.settings or {}, user_id=ident.user_id)
    session.add(group)
    await session.flush()
    await service.log_event(session, "group.created", message=group.name,
                            group_id=group.id, actor=ident.label)
    await session.commit()
    return await _group_payload(session, group)


@router.get("/groups/{group_id}")
async def get_group(group_id: str, session=Depends(db.get_db)):
    return await _group_payload(session, await _get_group(session, group_id))


@router.patch("/groups/{group_id}")
async def update_group(group_id: str, body: GroupUpdate,
                       ident: AdminIdentity = Depends(require_publishing_admin),
                       session=Depends(db.get_db)):
    group = await _get_group(session, group_id)
    if body.name is not None:
        group.name = body.name.strip()
    if body.enabled is not None:
        group.enabled = body.enabled
    if body.settings is not None:
        group.settings = body.settings
    await service.log_event(session, "group.updated", message=group.name,
                            group_id=group.id, actor=ident.label)
    await session.commit()
    return await _group_payload(session, group)


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str, confirm: bool = Query(False),
                       ident: AdminIdentity = Depends(require_publishing_admin),
                       session=Depends(db.get_db)):
    """Delete a group, its credentials, destinations and publication history.

    Requires ``?confirm=true`` and refuses while anything is still live. The
    cascade reaches the attempt rows, which are the only record that a post went
    out — deleting a group whose TikTok post is mid-flight would erase the
    evidence of a real publication. Disabling is almost always what is wanted.
    """
    group = await _get_group(session, group_id)
    if not confirm:
        raise HTTPException(
            400, "deleting a group also deletes its destinations and their "
                 "publication history. Disable it instead, or repeat with "
                 "?confirm=true.")
    live = (await session.execute(
        select(func.count()).select_from(PublishAttempt).where(
            PublishAttempt.publish_group_id == group.id,
            PublishAttempt.status.in_([state.PENDING, state.IN_FLIGHT,
                                       state.SUBMITTED, state.DEFERRED]))
    )).scalar_one()
    if live:
        raise HTTPException(
            409, f"{live} attempt(s) in this group are still queued or in "
                 "flight. Cancel them first.")
    name = group.name
    await session.delete(group)
    await service.log_event(session, "group.deleted", message=name,
                            actor=ident.label)
    await session.commit()
    return {"deleted": True, "name": name}


# --- Credentials ------------------------------------------------------------
# Everything below is the security boundary the whole feature rests on. The
# invariants, stated once:
#   * ``body.api_key`` is read, encrypted, and dropped. It is not logged, not
#     echoed, not stored in plaintext, and not put in an event message.
#   * responses go through ``views.credential_out``, which returns fingerprint
#     and last4 and nothing else.
#   * each group has its OWN credential; there is no global key.

@router.put("/groups/{group_id}/credential", status_code=201)
async def set_credential(group_id: str, body: CredentialCreate,
                         verify: bool = Query(True),
                         ident: AdminIdentity = Depends(require_publishing_admin),
                         session=Depends(db.get_db)):
    """Store (or rotate) this group's provider secret.

    The ONLY endpoint in the application that accepts a provider key. The
    plaintext lives in this function's frame and inside ``crypto.encrypt``; it is
    never written anywhere else, and the response cannot carry it.

    ``verify=true`` (the default) probes the provider first with a
    non-destructive call, so a typo is caught while the operator is still looking
    at the form instead of at 3 a.m. when 27 posts fail. The probe creates no post.
    """
    group = await _get_group(session, group_id)
    api_key = body.api_key  # plaintext, this frame only

    if body.kind == "api_key" and verify:
        provider = providers.get(group.provider)
        check = getattr(provider, "check_credential", None)
        if check is not None:
            result = await check(api_key)
            if not result.get("ok"):
                # 400 with the provider's reason, and NOTHING is stored: a key
                # the provider rejects is not worth an encrypted row that the
                # dispatcher will later discover is dead.
                raise HTTPException(
                    400, f"the provider rejected this key: "
                         f"{result.get('detail', 'unknown reason')}")

    aad = _credential_aad(group.id, body.kind, group.provider)
    sealed = crypto.encrypt(api_key, aad=aad)
    del api_key  # nothing below this line may touch the plaintext

    # Rotation is by-insert: the previous row is revoked, not overwritten, so a
    # historical attempt can still name the credential that signed it.
    prior = (await session.execute(
        select(PublishCredential).where(
            PublishCredential.publish_group_id == group.id,
            PublishCredential.kind == body.kind,
            PublishCredential.active.is_(True),
            PublishCredential.revoked_at.is_(None)))).scalars().all()
    for row in prior:
        row.active = False
        row.revoked_at = _now()

    cred = PublishCredential(
        publish_group_id=group.id, provider=group.provider, kind=body.kind,
        key_version=sealed["key_version"], nonce_b64=sealed["nonce_b64"],
        ciphertext_b64=sealed["ciphertext_b64"], aad=sealed["aad"],
        fingerprint=sealed["fingerprint"], last4=sealed["last4"], active=True)
    session.add(cred)
    await session.flush()

    await service.log_event(
        session, "credential.set",
        # The audit line records the fingerprint, never the key.
        message=f"{body.kind} rotated (fingerprint {sealed['fingerprint'][:8]})",
        group_id=group.id, actor=ident.label,
        data={"kind": body.kind, "last4": sealed["last4"],
              "replaced": len(prior)})
    await session.commit()
    return {"credential": views.credential_out(cred),
            "replaced": len(prior),
            "verified": bool(body.kind == "api_key" and verify)}


@router.get("/groups/{group_id}/credentials")
async def list_credentials(group_id: str, session=Depends(db.get_db)):
    """Masked credential history for one group, including revoked rows."""
    group = await _get_group(session, group_id)
    rows = (await session.execute(
        select(PublishCredential)
        .where(PublishCredential.publish_group_id == group.id)
        .order_by(PublishCredential.created_at.desc()))).scalars().all()
    return {"credentials": [views.credential_out(r) for r in rows]}


@router.post("/groups/{group_id}/credential/verify")
async def verify_credential(group_id: str, session=Depends(db.get_db)):
    """Re-probe the stored key without changing it.

    Answers "is this key still good?" — after a provider-side revocation, or to
    clear the ``invalid`` flag once the operator has fixed things upstream. Uses
    the same non-destructive probe as ``set_credential``; no post is created.
    """
    group = await _get_group(session, group_id)
    cred = await service.active_credential(session, group.id)
    if cred is None:
        raise HTTPException(404, "this group has no active API key")

    try:
        secret = crypto.decrypt({
            "key_version": cred.key_version, "nonce_b64": cred.nonce_b64,
            "ciphertext_b64": cred.ciphertext_b64, "aad": cred.aad})
    except Exception as e:
        # scrub() before this ever reaches a response or a log.
        raise HTTPException(500, crypto.scrub(str(e))[:300])

    provider = providers.get(group.provider)
    check = getattr(provider, "check_credential", None)
    if check is None:
        return {"ok": None, "detail": "this provider cannot verify a key "
                                      "without publishing."}
    result = await check(secret.reveal())
    if result.get("ok") and cred.invalid_at:
        # It works again — clear the flag so the dispatcher stops skipping it.
        cred.invalid_at = None
        cred.invalid_reason = None
        await session.commit()
    return {"ok": result.get("ok"), "detail": result.get("detail"),
            "credential": views.credential_out(cred)}


@router.delete("/groups/{group_id}/credential")
async def revoke_credential(group_id: str,
                            kind: str = Query("api_key"),
                            ident: AdminIdentity = Depends(require_publishing_admin),
                            session=Depends(db.get_db)):
    """Revoke this group's credential. Queued posts then park, not fail.

    Dispatch treats "no usable credential" as a deferral rather than a failure,
    so revoking a compromised key stops publishing without burning the retry
    budget of everything already queued.
    """
    group = await _get_group(session, group_id)
    rows = (await session.execute(
        select(PublishCredential).where(
            PublishCredential.publish_group_id == group.id,
            PublishCredential.kind == kind,
            PublishCredential.revoked_at.is_(None)))).scalars().all()
    for row in rows:
        row.active = False
        row.revoked_at = _now()
    await service.log_event(session, "credential.revoked",
                            message=f"{len(rows)} {kind} credential(s) revoked",
                            group_id=group.id, actor=ident.label)
    await session.commit()
    return {"revoked": len(rows)}


# --- Destinations -----------------------------------------------------------
@router.post("/groups/{group_id}/destinations", status_code=201)
async def create_destination(group_id: str, body: DestinationCreate,
                             ident: AdminIdentity = Depends(require_publishing_admin),
                             session=Depends(db.get_db)):
    """Register one connected social account as a publish target.

    Entered by hand, because Status 200 has no account-listing endpoint (every
    documented listing route returned 405 on probe). ``provider_account_ref`` is
    whatever the provider uses to address the account, and it is opaque here —
    nothing outside the adapter parses it.

    Health starts ``unverified`` and is proven by the first real publish. That is
    a provider limitation, not a shortcut: the only way to confirm a destination
    is to post to it, and doing that silently during setup would put content on a
    real audience.
    """
    group = await _get_group(session, group_id)
    platform = plat.normalize(body.platform)
    if not platform:
        raise HTTPException(400, "platform is required")

    caps = getattr(providers.get(group.provider), "capabilities", None)
    supported = list(getattr(caps, "platforms", ()) or ())
    if supported and platform not in supported:
        raise HTTPException(
            400, f"{group.provider} does not support '{platform}'. Supported: "
                 f"{', '.join(supported)}")

    dest = PublishDestination(
        publish_group_id=group.id, provider=group.provider, platform=platform,
        provider_account_ref=body.provider_account_ref.strip(),
        display_name=(body.display_name or "").strip() or None,
        enabled=body.enabled, health="unverified",
        health_detail="not yet confirmed by a publish",
        settings=body.settings or {})
    session.add(dest)
    try:
        await session.flush()
    except Exception:
        # uq_destination_identity: the same account twice in one group would
        # double-post every group publish, silently.
        await session.rollback()
        raise HTTPException(
            409, f"{platform} account '{body.provider_account_ref}' is already "
                 "registered in this group")

    await service.log_event(
        session, "destination.created",
        message=f"{platform}:{dest.provider_account_ref}",
        destination_id=dest.id, group_id=group.id, actor=ident.label)
    await session.commit()
    return views.destination_out(dest)


@router.patch("/destinations/{destination_id}")
async def update_destination(destination_id: str, body: DestinationUpdate,
                             ident: AdminIdentity = Depends(require_publishing_admin),
                             session=Depends(db.get_db)):
    """Rename, enable/disable, re-configure, or clear a bad health state.

    ``provider_account_ref`` is editable here because providers identify accounts
    by opaque strings whose format is not knowable in advance — Status 200 turned
    out to want a profile UUID while both its docs and its own "copy API ID"
    button offered the @handle — and a wrong one is rejected with a generic error
    too ambiguous to diagnose. Refused while work is in flight, because changing
    the target mid-post is undefined.

    ``reset_health`` clears ``blocked`` after fixing a disconnected account at
    the platform without deleting and recreating the destination (which would
    orphan its publication history).
    """
    dest = await session.get(PublishDestination,
                             _uuid_or_404(destination_id, "destination"))
    if dest is None:
        raise HTTPException(404, "destination not found")

    if body.provider_account_ref is not None:
        live = (await session.execute(
            select(func.count()).select_from(PublishAttempt).where(
                PublishAttempt.publish_destination_id == dest.id,
                PublishAttempt.status.in_([
                    state.PENDING, state.IN_FLIGHT,
                    state.SUBMITTED, state.DEFERRED])))
        ).scalar_one()
        if live:
            raise HTTPException(
                409, f"{live} attempt(s) for this destination are in flight. "
                     "Wait for them to finish, or disable the destination first.")
        old = dest.provider_account_ref
        dest.provider_account_ref = body.provider_account_ref.strip()
        await service.log_event(
            session, "destination.ref_changed",
            message=f"{dest.platform} {old} → {dest.provider_account_ref}",
            destination_id=dest.id,
            group_id=dest.publish_group_id, actor=ident.label)
    if body.display_name is not None:
        dest.display_name = body.display_name.strip() or None
    if body.enabled is not None:
        dest.enabled = body.enabled
    if body.settings is not None:
        dest.settings = body.settings
    if body.reset_health:
        dest.health = "unverified"
        dest.health_detail = "health reset by an admin"
        dest.cooldown_until = None
        # Clear the cached quota view too: a stale 'remaining: 0' from before the
        # fix would keep deferring posts the platform would now accept.
        dest.quota_remaining = None
        dest.quota_reset_at = None
        await service.log_event(session, "destination.health_reset",
                                destination_id=dest.id,
                                group_id=dest.publish_group_id,
                                actor=ident.label)
    await session.commit()
    return views.destination_out(dest)


@router.delete("/destinations/{destination_id}")
async def delete_destination(destination_id: str,
                             ident: AdminIdentity = Depends(require_publishing_admin),
                             session=Depends(db.get_db)):
    """Remove a destination. Refuses while it has live work.

    Deleting cascades to its attempts — the only record that a post went out — so
    a destination mid-publish is protected. Disabling keeps the history and stops
    future posts, which is what "remove this account" usually means.
    """
    dest = await session.get(PublishDestination,
                             _uuid_or_404(destination_id, "destination"))
    if dest is None:
        raise HTTPException(404, "destination not found")
    live = (await session.execute(
        select(func.count()).select_from(PublishAttempt).where(
            PublishAttempt.publish_destination_id == dest.id,
            PublishAttempt.status.in_([state.PENDING, state.IN_FLIGHT,
                                       state.SUBMITTED, state.DEFERRED]))
    )).scalar_one()
    if live:
        raise HTTPException(
            409, f"{live} attempt(s) for this destination are queued or in "
                 "flight. Cancel them, or disable the destination instead.")
    label = f"{dest.platform}:{dest.provider_account_ref}"
    group_id = dest.publish_group_id
    await session.delete(dest)
    await service.log_event(session, "destination.deleted", message=label,
                            group_id=group_id, actor=ident.label)
    await session.commit()
    return {"deleted": True, "destination": label}


# --- Scheduling -------------------------------------------------------------
# Assignments are the "N different clips per group per day" layer. N lives in the
# operator's request, never in this file.

@router.post("/groups/{group_id}/assignments", status_code=201)
async def create_assignments(group_id: str, body: AssignmentBulkCreate,
                             ident: AdminIdentity = Depends(require_publishing_admin),
                             session=Depends(db.get_db)):
    """Earmark clips for a group, optionally spread over the posting window.

    Creating an assignment publishes nothing: the reconciler turns it into a
    request when its time comes. That split is what lets a plan be reviewed, and
    deleted, before anything reaches a real account.
    """
    group = await _get_group(session, group_id)
    indexes = schedule.clip_selection(
        body.clip_count, clip_indexes=body.clip_indexes,
        max_clips=body.max_clips)
    if not indexes:
        raise HTTPException(400, "no clips selected")

    times = schedule.spread(
        len(indexes), now=_now(),
        spacing_seconds=body.spacing_seconds or schedule.DEFAULT_SPACING_SECONDS,
        start_at=body.start_at,
        respect_window=not body.immediate)

    result = await planner.create_assignments(
        session, group_id=group.id, job_id=body.job_id, clip_indexes=indexes,
        times=times, meta=body.meta)
    await service.log_event(
        session, "assignments.created",
        message=f"{len(result['created'])} clip(s) earmarked for {group.name}",
        group_id=group.id, actor=ident.label,
        data={"job_id": body.job_id, "clip_indexes": indexes})
    return {**result, "scheduled_for": times, "clip_indexes": indexes}


@router.get("/assignments")
async def list_assignments(group_id: Optional[str] = None,
                           job_id: Optional[str] = None,
                           status: Optional[str] = None,
                           limit: int = Query(200, ge=1, le=1000),
                           session=Depends(db.get_db)):
    stmt = select(PublishAssignment)
    if group_id:
        stmt = stmt.where(PublishAssignment.publish_group_id
                          == _uuid_or_404(group_id, "group"))
    if job_id:
        stmt = stmt.where(PublishAssignment.job_id == job_id)
    if status:
        stmt = stmt.where(PublishAssignment.status == status)
    rows = (await session.execute(
        stmt.order_by(PublishAssignment.scheduled_for.asc().nullsfirst())
        .limit(limit))).scalars().all()
    return {"assignments": [views.assignment_out(r) for r in rows],
            "count": len(rows)}


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(assignment_id: str,
                            ident: AdminIdentity = Depends(require_publishing_admin),
                            session=Depends(db.get_db)):
    """Un-assign a clip. Never touches a request it already produced.

    A `requested` assignment has already become a live publish request, so
    deleting the row here would look like a cancel while the post went out
    anyway. Cancel the request instead.
    """
    row = await session.get(PublishAssignment,
                            _uuid_or_404(assignment_id, "assignment"))
    if row is None:
        raise HTTPException(404, "assignment not found")
    if row.status == "requested":
        raise HTTPException(
            409, "this assignment already produced a publish request; cancel "
                 f"request {row.publish_request_id} instead")
    await session.delete(row)
    return {"deleted": True}


@router.get("/groups/{group_id}/capacity")
async def group_capacity(group_id: str, session=Depends(db.get_db)):
    """Free daily slots per platform, from the provider's own quota view.

    `remaining: null` means the provider has not told us yet — quota arrives on a
    response header, so before the day's first post there is nothing to report.
    """
    group = await _get_group(session, group_id)
    return {"group_id": str(group.id),
            "platforms": await planner.assignment_capacity(session, group.id)}


@router.post("/schedule/run")
async def run_scheduler_now(ident: AdminIdentity = Depends(require_publishing_admin)):
    """Force a scheduler pass instead of waiting for the reconciler tick.

    Operationally useful ("I fixed the destination, go now") and it is how the
    assignment path is exercised in a test without a 60-second sleep.
    """
    async with db.session() as session:
        async with session.begin():
            converted = await planner.run_due_assignments(session)
    return {"converted": converted}


# --- Audit ------------------------------------------------------------------
@router.get("/events")
async def list_events(group_id: Optional[str] = None,
                      kind: Optional[str] = None,
                      limit: int = Query(100, ge=1, le=500),
                      session=Depends(db.get_db)):
    """The append-only audit trail.

    Exists so "why did this post go out twice / not at all" is answerable from
    the database months later, independent of application logs — which rotate.
    """
    stmt = select(PublishEvent)
    if group_id:
        stmt = stmt.where(PublishEvent.publish_group_id
                          == _uuid_or_404(group_id, "group"))
    if kind:
        stmt = stmt.where(PublishEvent.kind == kind)
    rows = (await session.execute(
        stmt.order_by(PublishEvent.created_at.desc()).limit(limit))
    ).scalars().all()
    return {"events": [views.event_out(r) for r in rows], "count": len(rows)}


@router.get("/dry-run")
async def dry_run_log():
    """What the fake provider recorded, when PUBLISHING_DRY_RUN is on.

    Lets an operator (or a test) confirm the full pipeline reached "submit" with
    the right account, caption and media ref — without a credential and without a
    real post. 409 when dry-run is off, so this can never be mistaken for real
    publication history.
    """
    if not settings.dry_run:
        raise HTTPException(409, "PUBLISHING_DRY_RUN is not enabled")
    from .providers import fake
    return {"submissions": list(fake.submissions), "uploads": list(fake.uploads)}


@router.post("/dry-run/reset")
async def dry_run_reset():
    if not settings.dry_run:
        raise HTTPException(409, "PUBLISHING_DRY_RUN is not enabled")
    from .providers import fake
    fake.reset()
    return {"reset": True}
