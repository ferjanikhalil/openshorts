"""Turning "publish this job's clips" into scheduled requests.

Two entry points, one code path:

  * ``plan_job`` — called from the autopilot seam when a job's clips finish
    styling, and from the admin API's "publish this whole job" action.
  * ``run_due_assignments`` — the clock side. Assignments earmarked for a group
    become requests when their time comes.

Both end at ``service.create_request``, so a scheduled post and a hand-clicked
one are the same kind of object with the same idempotency guarantee. That is the
point of putting the planning here rather than in the worker: scheduling decides
*when*, it does not get its own publishing pipeline.
"""
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select

from . import clips as clips_mod, schedule, service, state
from .models import PublishAssignment, PublishDestination


def _now():
    return datetime.now(timezone.utc)


async def plan_job(session, *, job_id: str, clip_count: int, plan: dict,
                   user_id=None, actor: str = "planner") -> dict:
    """Create one request per selected clip, spaced over the posting window.

    ``plan`` is the shape ``autopilot.register_batch`` documents:
    ``destination_ids`` / ``group_ids`` (at least one), plus optional
    ``platforms``, ``clip_indexes``, ``max_clips``, ``spacing_seconds`` and
    ``immediate``.

    Returns a report rather than raising on a partial outcome: with 9 accounts
    and 3 clips there are many ways for *part* of this to be impossible, and the
    caller (a background thread, hours after the user left) can only log what
    happened.
    """
    indexes = schedule.clip_selection(
        clip_count, clip_indexes=plan.get("clip_indexes"),
        max_clips=plan.get("max_clips"))
    if not indexes:
        return {"ok": True, "created": [], "existing": [], "skipped": [],
                "reason": "no clips selected"}

    destinations = await service.expand_destinations(
        session,
        destination_ids=plan.get("destination_ids") or (),
        group_ids=plan.get("group_ids") or (),
        platforms=plan.get("platforms"))
    if not destinations:
        return {"ok": False, "created": [], "existing": [], "skipped": indexes,
                "reason": "the plan resolved to no enabled destinations"}

    now = _now()
    times = schedule.spread(
        len(indexes), now=now,
        spacing_seconds=int(plan.get("spacing_seconds")
                            or schedule.DEFAULT_SPACING_SECONDS),
        respect_window=not plan.get("immediate"))

    # ``created`` is work this call inserted; ``existing`` is a request the
    # idempotency key already pointed at. The split matters: a repeat Auto Post
    # for the same job/clip/destinations/schedule reuses the old request (so no
    # duplicate post goes out), and reporting that as "queued" is a lie — the
    # dispatcher will never pick up new work, because none was created.
    created, existing, skipped = [], [], []
    for slot, clip_index in enumerate(indexes):
        info = clips_mod.resolve(job_id, clip_index)
        if info is None or not info.get("filename"):
            # The clip never rendered, or retention already removed it. Recorded
            # rather than retried: no amount of waiting brings the bytes back.
            skipped.append({"clip_index": clip_index, "reason": "clip unavailable"})
            continue

        payload = _payload_for(info, plan)
        req, was_created = await service.create_request(
            session, job_id=job_id, clip_index=clip_index,
            destinations=destinations, payload=payload,
            mode="scheduled" if times[slot] else "group",
            scheduled_for=times[slot],
            content_fingerprint=info.get("fingerprint"),
            user_id=user_id, actor=actor, return_created=True)
        item = {"request_id": str(req.id), "clip_index": clip_index,
                "scheduled_for": req.scheduled_for,
                "destinations": len(destinations),
                "status": req.status}
        (created if was_created else existing).append(item)

    await service.log_event(
        session, "planner.planned",
        message=f"{len(created)} request(s) for job {job_id}",
        actor=actor,
        data={"job_id": job_id, "created": len(created),
              "existing": len(existing), "skipped": len(skipped),
              "destinations": [str(d.id) for d in destinations]})
    return {"ok": True, "created": created, "existing": existing,
            "skipped": skipped}


def _payload_for(info: dict, plan: dict) -> dict:
    """Freeze the text this request will publish.

    Per-platform text comes from the clip (the pipeline writes one per platform)
    unless the plan overrides it. Frozen here, at creation, so a later recipe
    edit cannot rewrite what a scheduled post says.
    """
    payload = {
        "title": plan.get("title") or info.get("title") or "",
        "caption": plan.get("caption") or info.get("caption") or "",
        "per_platform": dict(info.get("per_platform") or {}),
        "resolved_at": _now().isoformat(),
        "source": plan.get("source") or "planner",
    }
    for platform, override in (plan.get("per_platform") or {}).items():
        merged = dict(payload["per_platform"].get(platform) or {})
        merged.update(override or {})
        payload["per_platform"][platform] = merged
    return payload


# --- Assignment side --------------------------------------------------------
async def create_assignments(session, *, group_id, job_id: str,
                             clip_indexes: List[int],
                             times: Optional[List[Optional[datetime]]] = None,
                             user_id=None, meta: Optional[dict] = None) -> dict:
    """Earmark clips for a group without publishing them.

    The UNIQUE ``(group, job, clip)`` constraint makes re-running the planner
    idempotent, so a daily job can be re-run after a partial failure without
    double-booking a clip.
    """
    gid = service._as_uuid(group_id)
    created, existing = [], []
    for slot, clip_index in enumerate(clip_indexes):
        already = (await session.execute(
            select(PublishAssignment).where(
                PublishAssignment.publish_group_id == gid,
                PublishAssignment.job_id == job_id,
                PublishAssignment.clip_index == clip_index)
        )).scalar_one_or_none()
        if already is not None:
            existing.append(str(already.id))
            continue
        info = clips_mod.resolve(job_id, clip_index) or {}
        row = PublishAssignment(
            publish_group_id=gid, user_id=user_id, job_id=job_id,
            clip_index=clip_index, status="pending",
            content_fingerprint=info.get("fingerprint"),
            scheduled_for=(times[slot] if times and slot < len(times) else None),
            meta=meta or None)
        session.add(row)
        await session.flush()
        created.append(str(row.id))
    return {"created": created, "existing": existing}


async def run_due_assignments(session, limit: int = 50) -> int:
    """Convert assignments whose time has come into requests.

    Claimed with ``FOR UPDATE SKIP LOCKED`` for the same reason the attempt queue
    is: two app replicas running the same reconciler tick must not both turn one
    assignment into a request, and the idempotency key would collapse them only
    if both used the same destination set — which is not guaranteed while
    destinations are being edited.
    """
    now = _now()
    rows = (await session.execute(
        select(PublishAssignment)
        .where(PublishAssignment.status == "pending")
        .where((PublishAssignment.scheduled_for.is_(None))
               | (PublishAssignment.scheduled_for <= now))
        .order_by(PublishAssignment.scheduled_for.asc().nullsfirst())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    done = 0
    for row in rows:
        dests = (await session.execute(
            select(PublishDestination).where(
                PublishDestination.publish_group_id == row.publish_group_id,
                PublishDestination.enabled.is_(True))
        )).scalars().all()
        usable = [d for d in dests if d.health not in ("blocked", "disconnected")]
        if not usable:
            # Left pending on purpose: the group's accounts may be reconnected
            # later, and cancelling here would silently drop the plan.
            await service.log_event(
                session, "assignment.no_destinations",
                message=f"assignment {row.id} has no usable destination",
                group_id=row.publish_group_id)
            continue

        info = clips_mod.resolve(row.job_id, row.clip_index)
        if info is None or not info.get("filename"):
            row.status = "cancelled"
            await service.log_event(
                session, "assignment.clip_missing",
                message=f"clip {row.job_id}[{row.clip_index}] is no longer available",
                group_id=row.publish_group_id)
            done += 1
            continue

        req = await service.create_request(
            session, job_id=row.job_id, clip_index=row.clip_index,
            destinations=usable,
            payload=_payload_for(info, (row.meta or {})),
            mode="scheduled", scheduled_for=row.scheduled_for,
            content_fingerprint=info.get("fingerprint"),
            user_id=row.user_id, actor="scheduler")
        row.status = "requested"
        row.publish_request_id = req.id
        done += 1
    return done


async def assignment_capacity(session, group_id) -> dict:
    """How many slots a group has left today, per platform.

    Derived from the destinations' cached quota rather than a configured number:
    the provider is the authority on the cap, and hard-coding one here would make
    the operator's plan tier a constant in the code.
    """
    gid = service._as_uuid(group_id)
    rows = (await session.execute(
        select(PublishDestination).where(
            PublishDestination.publish_group_id == gid,
            PublishDestination.enabled.is_(True))
    )).scalars().all()
    out = {}
    now = _now()
    for dest in rows:
        fresh = dest.quota_reset_at is None or dest.quota_reset_at > now
        out[dest.platform] = {
            "destination_id": str(dest.id),
            "remaining": dest.quota_remaining if fresh else None,
            "limit": dest.quota_limit,
            "reset_at": dest.quota_reset_at,
            "health": dest.health,
        }
    return out


def clips_still_publishable(statuses) -> bool:
    """True while any attempt of a plan could still change outcome."""
    return any(s not in state.TERMINAL_STATES for s in statuses)
