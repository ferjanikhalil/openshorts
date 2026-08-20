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
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select

from . import clips as clips_mod, schedule, service, state
from .models import (
    PublishAssignment, PublishAttempt, PublishDestination, PublishGroup,
)


def _now():
    return datetime.now(timezone.utc)


def group_plan(group: PublishGroup) -> Optional[dict]:
    """The group's normalized posting rhythm, or None when it has none."""
    try:
        return schedule.normalize_plan((group.settings or {}).get("plan"))
    except ValueError:
        # A malformed plan must not break the reconciler; the group's posts
        # simply run as plain assignments until an admin fixes it. The admin
        # API rejects malformed plans on write, so this is a race, not a norm.
        return None


async def plan_job(session, *, job_id: str, clip_count: int, plan: dict,
                   user_id=None, actor: str = "planner") -> dict:
    """Create one request per selected clip, spaced over the posting window.

    ``plan`` is the shape ``autopilot.register_batch`` documents:
    ``destination_ids`` / ``group_ids`` (at least one), plus optional
    ``platforms``, ``clip_indexes``, ``max_clips``, ``spacing_seconds`` and
    ``immediate``. ``schedule: "rhythm"`` earmarks the group-targeted clips as
    assignments instead — each group then places them on its own posting plan
    (start time, interval, daily cap) batch-wide, which is how a multi-video
    autopilot run avoids every video colliding on the same slots.

    Returns a report rather than raising on a partial outcome: with 9 accounts
    and 3 clips there are many ways for *part of this* to be impossible, and
    the caller (a background thread, hours after the user left) can only log
    what happened.
    """
    indexes = schedule.clip_selection(
        clip_count, clip_indexes=plan.get("clip_indexes"),
        max_clips=plan.get("max_clips"))
    if not indexes:
        return {"ok": True, "created": [], "existing": [], "skipped": [],
                "reason": "no clips selected"}

    if plan.get("schedule") == "rhythm" and plan.get("group_ids"):
        return await _plan_rhythm(
            session, job_id=job_id, indexes=indexes, plan=plan,
            user_id=user_id, actor=actor)

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


async def _plan_rhythm(session, *, job_id: str, indexes: List[int],
                       plan: dict, user_id=None, actor: str = "planner"
                       ) -> dict:
    """Rhythm planning: earmark clips per group; slots come from each group's
    posting plan, assigned by the reconciler (``assign_rhythm_slots``).

    Deliberately NOT spread here: the group may already have queued clips from
    an earlier autopilot run, and only the slot assigner sees the whole queue —
    computing times now would collide with it. ``meta`` freezes the payload and
    platform filter exactly as ``_payload_for`` would, so conversion later
    publishes what was reviewed at submit time.
    """
    created, existing, skipped = [], [], []
    group_ids = list(plan.get("group_ids") or [])
    groups = (await session.execute(
        select(PublishGroup).where(PublishGroup.id.in_(
            [service._as_uuid(g) for g in group_ids]))
    )).scalars().all()

    for group in groups:
        if not group.enabled:
            skipped.append({"clip_index": None,
                            "reason": f"group {group.name} is disabled"})
            continue
        for clip_index in indexes:
            info = clips_mod.resolve(job_id, clip_index)
            if info is None or not info.get("filename"):
                skipped.append({"clip_index": clip_index,
                                "reason": "clip unavailable"})
                continue
            meta = {"await_slot": True,
                    "payload": _payload_for(info, plan),
                    "platforms": list(plan.get("platforms") or []) or None,
                    "source": plan.get("source") or "planner"}
            result = await create_assignments(
                session, group_id=group.id, job_id=job_id,
                clip_indexes=[clip_index], user_id=user_id, meta=meta)
            for aid in result["created"]:
                created.append({"assignment_id": aid, "group": group.name,
                                "clip_index": clip_index, "job_id": job_id})
            existing.extend(result["existing"])

    await service.log_event(
        session, "planner.planned_rhythm",
        message=f"{len(created)} clip(s) earmarked for group rhythms "
                f"({job_id})",
        actor=actor,
        data={"job_id": job_id, "created": len(created),
              "existing": len(existing), "skipped": len(skipped),
              "groups": [str(g.id) for g in groups]})
    return {"ok": True, "created": created, "existing": existing,
            "skipped": skipped, "mode": "rhythm"}


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


async def assign_rhythm_slots(session, limit: int = 200) -> int:
    """Place ``await_slot`` assignments onto their group's rhythm.

    The assigner is the only thing that sees the group's WHOLE queue — this run's
    clips, an earlier autopilot run's clips, whatever is still pending — so it
    is the only place slots can be allocated without collisions. Runs on the
    reconciler tick; bookings (already-slotted assignments and future requests
    for the group) count against each day's cap, and the cached provider quota
    tightens the cap further when it is known.

    Returns how many assignments received a slot.
    """
    rows = (await session.execute(
        select(PublishAssignment)
        .where(PublishAssignment.status == "pending")
        .where(PublishAssignment.scheduled_for.is_(None))
        .order_by(PublishAssignment.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    by_group: dict = {}
    for row in rows:
        meta = row.meta or {}
        if not meta.get("await_slot"):
            continue  # a plain assignment: converted as-is when due
        by_group.setdefault(row.publish_group_id, []).append(row)

    assigned = 0
    for group_id, group_rows in by_group.items():
        group = await session.get(PublishGroup, group_id)
        if group is None:
            continue
        plan = group_plan(group)
        if plan is None:
            # The plan was removed after clips were earmarked. Publish them as
            # soon as their slot-less conversion allows — stalling silently
            # would be a plan edit eating real content.
            for row in group_rows:
                row.meta = {k: v for k, v in (row.meta or {}).items()
                            if k != "await_slot"}
            continue

        booked = await _group_bookings(session, group_id)
        daily_cap = await _group_daily_cap(session, group_id, plan)
        now = _now()
        result = schedule.rhythm_slots(
            plan, len(group_rows), now=now, booked=booked,
            daily_cap=daily_cap, group_id=str(group_id))
        for row, slot in zip(group_rows, result["slots"]):
            row.scheduled_for = slot
            assigned += 1
        await service.log_event(
            session, "assignment.slots_assigned",
            message=(f"{len(result['slots'])} slot(s) on {group.name}'s "
                     f"rhythm ({plan['start_time']} every "
                     f"{plan['interval_hours']}h, cap {plan['max_per_day']}/day)"),
            group_id=group_id,
            data={"per_day": result["per_day"],
                  "daily_cap": daily_cap})
    if assigned:
        await session.flush()
    return assigned


async def _group_bookings(session, group_id) -> list:
    """Times already committed against this group: slotted assignments plus
    future-scheduled requests that fan out to it. Both count toward the daily
    cap — the provider's quota does not care who booked the slot."""
    from .models import PublishRequest
    times = (await session.execute(
        select(PublishAssignment.scheduled_for).where(
            PublishAssignment.publish_group_id == group_id,
            PublishAssignment.scheduled_for.is_not(None),
            PublishAssignment.status == "pending")
    )).scalars().all()
    req_times = (await session.execute(
        select(PublishRequest.scheduled_for)
        .join(PublishAttempt,
              PublishAttempt.publish_request_id == PublishRequest.id)
        .where(PublishAttempt.publish_group_id == group_id,
               PublishRequest.scheduled_for.is_not(None))
        .distinct()
    )).scalars().all()
    return [t for t in list(times) + list(req_times) if t is not None]


async def _group_daily_cap(session, group_id, plan: dict) -> Optional[int]:
    """min(plan cap, tightest known provider quota limit) for the group."""
    dests = (await session.execute(
        select(PublishDestination).where(
            PublishDestination.publish_group_id == group_id,
            PublishDestination.enabled.is_(True))
    )).scalars().all()
    limits = [d.quota_limit for d in dests if d.quota_limit]
    if not limits:
        return plan["max_per_day"]
    return min(plan["max_per_day"], min(limits))


async def run_due_assignments(session, limit: int = 50) -> int:
    """Turn assignments into requests.

    Slotted assignments convert as soon as they have a slot — including FUTURE
    slots. With remote scheduling the promote pass then hands them to the
    provider early; without it the attempt simply waits on the slot. Either
    way the request (and its audit trail) exists from slot time being known,
    which is what the activity page should show.

    A slot that passed while this machine was down is resolved by the group's
    catch-up policy: re-slot it, skip it, or publish immediately.

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
        # Slotted work first (due before future): it is convertible now.
        # Slot-less rhythm rows sort last so a large await_slot backlog cannot
        # starve the limit away from rows that actually convert.
        .order_by(PublishAssignment.scheduled_for.asc().nullslast())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    done = 0
    plans: dict = {}
    for row in rows:
        meta = row.meta or {}
        if meta.get("await_slot") and row.scheduled_for is None:
            # Rhythm assignment waiting for the slot assigner.
            continue

        # --- catch-up: the slot passed while nobody was awake ----------------
        if (row.scheduled_for is not None
                and row.scheduled_for < now - timedelta(
                    seconds=schedule.CATCH_UP_GRACE_SECONDS)):
            if row.publish_group_id not in plans:
                group = await session.get(PublishGroup,
                                          row.publish_group_id)
                plans[row.publish_group_id] = (
                    group_plan(group) if group else None) or {
                    "catch_up": "next_slot"}
            policy = plans[row.publish_group_id].get("catch_up",
                                                     "next_slot")
            if policy == "skip":
                row.status = "cancelled"
                await service.log_event(
                    session, "assignment.catchup_skipped",
                    message=f"slot {row.scheduled_for.isoformat()} passed; "
                            f"policy skip",
                    group_id=row.publish_group_id)
                done += 1
                continue
            if policy == "next_slot":
                passed = row.scheduled_for
                row.scheduled_for = None
                await service.log_event(
                    session, "assignment.catchup_reslotted",
                    message=f"slot passed while offline; re-slotting "
                            f"(was {passed.isoformat()})",
                    group_id=row.publish_group_id)
                continue

        wanted = {str(p).lower() for p in meta.get("platforms") or ()} or None
        dests = (await session.execute(
            select(PublishDestination).where(
                PublishDestination.publish_group_id == row.publish_group_id,
                PublishDestination.enabled.is_(True))
        )).scalars().all()
        usable = [d for d in dests
                  if d.health not in ("blocked", "disconnected")
                  and (not wanted or d.platform.lower() in wanted)]
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

        payload = meta.get("payload") or _payload_for(info, meta)
        req = await service.create_request(
            session, job_id=row.job_id, clip_index=row.clip_index,
            destinations=usable,
            payload=payload,
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
