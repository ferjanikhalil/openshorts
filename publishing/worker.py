"""Background loops.

Three loops, matching the four ``while True: await asyncio.sleep()`` loops the
app already runs — no broker, no Celery, no Redis. The queue is a table and
``FOR UPDATE SKIP LOCKED`` is the claim, which is the same mechanism
``cloud/metering.py`` already relies on.

  dispatcher  claims due attempts and publishes them
  reconciler  drains webhooks, ages out stale submissions, refreshes media refs,
              and turns due assignments into requests
  transfer    stages clips in the object store ahead of their slots, and sweeps
              the ones nothing needs any more (only when a store is configured)

The scheduler shares the reconciler's tick instead of getting a loop of its own:
an assignment's granularity is minutes, so a 60-second pass is already far finer
than the thing it schedules, and one fewer loop is one fewer thing to keep alive.
The transfer loop does NOT share a tick, because that is the point of it: one
upload can occupy twenty minutes of wall clock on a slow uplink, and anything
sharing its tick would stop dead for the duration.

Each loop swallows its own exceptions. A loop that dies takes publishing down
silently until the next redeploy, which is a worse failure than a logged error.
"""
import asyncio
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import db, dispatcher, planner, service, webhooks
from .config import (MEDIA_REFRESH_MARGIN_SECONDS, STORE_SWEEP_INTERVAL_SECONDS,
                     settings)
from .models import PublishMedia

_tasks = []
_worker_id = None


def worker_id() -> str:
    """Stable per-process id, so a claim can be attributed to a replica."""
    global _worker_id
    if _worker_id is None:
        _worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    return _worker_id


def _now():
    return datetime.now(timezone.utc)


async def recover_stale_on_boot() -> int:
    """Clean up what a previous process left mid-flight. Returns how many.

    Only IN_FLIGHT rows are re-queued, and only ones whose claim has gone quiet
    long enough to be abandoned rather than merely slow — see
    ``service.recover_orphaned_claims``. SUBMITTED rows are deliberately left
    alone: those reached the provider, and re-queuing one would republish a post
    that may already be live. The stale sweeper resolves them to ``unknown``
    instead, where a human decides.
    """
    try:
        async with db.session() as session:
            async with session.begin():
                orphans = await service.recover_orphaned_claims(session)
        if orphans:
            print(f"📡 Publishing: re-queued {orphans} orphaned attempt(s).")
        return orphans
    except Exception as e:
        print(f"⚠️  Publishing: boot recovery failed: {e}")
        return 0


def start_loops() -> None:
    if _tasks:
        return
    _tasks.append(asyncio.create_task(_dispatch_loop()))
    _tasks.append(asyncio.create_task(_reconcile_loop()))
    _tasks.append(asyncio.create_task(_transfer_loop()))


def stop_loops() -> None:
    for task in _tasks:
        task.cancel()
    _tasks.clear()


async def _dispatch_loop() -> None:
    interval = settings.dispatch_interval_seconds
    while True:
        try:
            handled = await dispatch_once()
            # Back off only when idle: a full batch means more work is waiting.
            await asyncio.sleep(0 if handled else interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️  Publishing dispatcher error: {e}")
            await asyncio.sleep(interval)


async def dispatch_once(limit: int = 10) -> int:
    """Claim and run one batch of due attempts. Returns how many ran."""
    owner = worker_id()
    async with db.session() as session:
        async with session.begin():
            attempts = await service.claim_due_attempts(
                session, owner, limit=limit)
            claimed_ids = [a.id for a in attempts]

    if not claimed_ids:
        return 0

    # Each attempt gets its own transaction: one poisoned attempt must not roll
    # back the outcomes of the others in the batch, including ones that already
    # produced a real post.
    for attempt_id in claimed_ids:
        try:
            async with db.session() as session:
                async with session.begin():
                    from .models import PublishAttempt
                    attempt = await session.get(PublishAttempt, attempt_id)
                    if attempt is None:
                        continue
                    # The claim was committed above, in a transaction that has
                    # since closed, and this batch is worked serially — so the
                    # last row can wait minutes for its turn. Passing the owner
                    # makes dispatch re-check that the row is still ours before
                    # it submits anything; another process's recovery pass may
                    # have taken it over in the meantime.
                    await dispatcher.dispatch_attempt(
                        session, attempt, claim_owner=owner)
        except Exception as e:
            print(f"⚠️  Publishing: attempt {attempt_id} raised: {e}")
            await _fail_open(attempt_id, str(e))
    return len(claimed_ids)


async def _fail_open(attempt_id, message: str) -> None:
    """Never leave an attempt stuck IN_FLIGHT after an unexpected exception.

    The exception happened in OUR code, before or around the provider call, so
    the safe resolution is `unknown` only if we know a submit went out. We do
    not, so the attempt is returned to pending with a backoff — the provider-call
    path itself already classifies its own ambiguity.
    """
    try:
        async with db.session() as session:
            async with session.begin():
                from .models import PublishAttempt
                from . import state
                attempt = await session.get(PublishAttempt, attempt_id)
                if attempt is None or attempt.status != state.IN_FLIGHT:
                    return
                attempt.status = state.UNKNOWN
                attempt.completed_at = _now()
                attempt.error_code = "dispatch_exception"
                attempt.error_message = (
                    f"dispatcher raised before the outcome was recorded: "
                    f"{message}"[:2000])
                await service.log_event(
                    session, "attempt.unknown",
                    message="dispatcher exception",
                    request_id=attempt.publish_request_id,
                    attempt_id=attempt.id,
                    destination_id=attempt.publish_destination_id,
                    group_id=attempt.publish_group_id)
                await service.refresh_request_status(
                    session, attempt.publish_request_id)
    except Exception as e:  # pragma: no cover - defensive
        print(f"⚠️  Publishing: could not resolve stuck attempt "
              f"{attempt_id}: {e}")


async def _reconcile_loop() -> None:
    interval = settings.reconcile_interval_seconds
    while True:
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️  Publishing reconciler error: {e}")
        await asyncio.sleep(interval)


async def reconcile_once() -> dict:
    """One reconciliation pass. Returns what it did, for tests and metrics."""
    out = {"webhooks": 0, "stale": 0, "media_expired": 0, "assignments": 0,
           "slots": 0, "media_preregistered": 0, "promoted": 0}

    out["webhooks"] = await webhooks.drain_pending()

    async with db.session() as session:
        async with session.begin():
            out["stale"] = await service.sweep_stale_submitted(session)
            out["media_expired"] = await _purge_expired_media(session)

    # Its own transaction: creating work is the one thing in this pass that
    # writes new rows, and it must not be rolled back by a sweeper error.
    try:
        async with db.session() as session:
            async with session.begin():
                out["slots"] = await planner.assign_rhythm_slots(session)
                out["assignments"] = await planner.run_due_assignments(session)
                out["promoted"] = await dispatcher.promote_remote_schedules(
                    session)
    except Exception as e:
        print(f"⚠️  Publishing scheduler error: {e}")

    # Media pre-registration does provider I/O (a full-clip transfer) inside its
    # transaction, so it gets its own session and never blocks the sweeps above.
    try:
        async with db.session() as session:
            async with session.begin():
                out["media_preregistered"] = (
                    await dispatcher.preregister_next_media(session))
    except Exception as e:
        print(f"⚠️  Publishing media pre-registration error: {e}")
    return out


async def _purge_expired_media(session) -> int:
    """Drop provider media refs at or past expiry.

    Deleting the row is the whole fix: the next dispatch finds no cache entry and
    re-uploads. Keeping a dead ref would make a scheduled post fail with an
    unfetchable-media error days after the clip was fine.

    The staged object in our own store is deliberately left alone — the re-upload
    to the provider needs it, and re-staging a 20 MB clip is the expensive half.
    ``_sweep_store`` retires objects on their own schedule.
    """
    cutoff = _now() + timedelta(seconds=MEDIA_REFRESH_MARGIN_SECONDS)
    rows = (await session.execute(
        select(PublishMedia).where(PublishMedia.expires_at.is_not(None),
                                   PublishMedia.expires_at <= cutoff)
    )).scalars().all()
    for row in rows:
        await session.delete(row)
    return len(rows)


# --- Object-store staging ---------------------------------------------------
# A clip whose upload keeps failing must not sit at the head of the queue
# blocking every clip behind it. In memory because it is a pacing hint, not
# state: dispatch still parks the attempt with a visible reason, and a restart
# retrying immediately is the right behaviour.
_transfer_failures: dict = {}
_TRANSFER_RETRY_SECONDS = 300.0
_last_store_sweep = 0.0


async def _transfer_loop() -> None:
    """Stage clips in the object store, one at a time, ahead of their slots."""
    interval = settings.transfer_interval_seconds
    while True:
        try:
            moved = await transfer_once()
            # A completed transfer means there may be more waiting; only an idle
            # pass sleeps.
            await asyncio.sleep(0 if moved else interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️  Publishing media transfer error: {e}")
            await asyncio.sleep(interval)


async def transfer_once() -> int:
    """Copy ONE not-yet-staged clip into the object store. Returns 0 or 1.

    One per pass, serially, for the same reason the dispatcher paces media
    registrations: the uplink this exists to work around serves exactly one
    large file at a time, poorly. The upload itself runs in a thread with no
    database transaction open — it can take twenty minutes, and no transaction
    should live that long.
    """
    from . import clips, media, objectstore
    if not media.store_available():
        return 0

    await _sweep_store_if_due()

    candidates = await _clips_awaiting_transfer()
    now = time.monotonic()
    for cand in candidates:
        key = objectstore.object_key(
            cand["job_id"], cand["clip_index"], cand["fingerprint"])
        failed = _transfer_failures.get(key)
        if failed and failed["until"] > now:
            continue

        info = clips.resolve(cand["job_id"], cand["clip_index"])
        if not info or not info.get("filename") or not info.get("output_dir"):
            # No local bytes. Two very different causes, and staging cannot tell
            # them apart nor needs to: the clip is genuinely gone (dispatch
            # records the permanent failure), or this is a publisher-only
            # instance that never had the files (dispatch parks until the owning
            # instance stages them). Skipping without recording anything is
            # right either way — a stage_failed here would be this process
            # blaming the store for a file it was never given.
            continue
        path = media.clip_local_path(
            info["output_dir"], cand["job_id"], info["filename"])
        if not path or not os.path.exists(path):
            continue
        # The key is content-addressed, so a re-styled clip (new bytes, new
        # fingerprint) is a different object. Stage the fingerprint the
        # dispatcher will actually ask for.
        live_key = objectstore.object_key(
            cand["job_id"], cand["clip_index"],
            info.get("fingerprint") or cand["fingerprint"])

        try:
            if await asyncio.to_thread(objectstore.head, live_key):
                continue  # already staged
        except objectstore.StoreError as e:
            # The store itself is unreachable: every other candidate would fail
            # the same way, so stop the pass instead of hammering it.
            print(f"⚠️  Publishing: media store unreachable: {e}")
            return 0

        size = info.get("size_bytes") or 0
        print(f"📤 Publishing: staging {cand['job_id']}[{cand['clip_index']}] "
              f"({size / 1048576:.1f} MB) in the media store…")
        started = time.monotonic()
        try:
            uploaded = await asyncio.to_thread(
                objectstore.upload, path, live_key)
        except objectstore.StoreError as e:
            _transfer_failures[live_key] = {
                "until": time.monotonic() + _TRANSFER_RETRY_SECONDS,
                "error": str(e)}
            print(f"⚠️  Publishing: staging failed for {cand['job_id']}"
                  f"[{cand['clip_index']}]: {e}")
            await _log_transfer(cand, "media.stage_failed", str(e)[:500])
            return 0

        elapsed = max(time.monotonic() - started, 0.001)
        _transfer_failures.pop(live_key, None)
        rate = uploaded / elapsed / 1024
        print(f"✅ Publishing: staged {cand['job_id']}[{cand['clip_index']}] "
              f"in {elapsed:.0f}s ({rate:.0f} KB/s)")
        await _log_transfer(
            cand, "media.staged",
            f"{uploaded / 1048576:.1f} MB uploaded in {elapsed:.0f}s "
            f"({rate:.0f} KB/s)")
        return 1
    return 0


async def _clips_awaiting_transfer(limit: int = 40) -> list:
    """Clips belonging to queued posts, nearest slot first.

    Same selection as ``dispatcher.preregister_next_media`` — staging has to
    follow the order posts will actually go out in, or a clip due in five minutes
    waits behind one due tomorrow.
    """
    from . import state
    from .models import PublishAttempt, PublishRequest

    out, seen = [], set()
    async with db.session() as session:
        rows = (await session.execute(
            select(PublishAttempt.publish_group_id, PublishRequest.job_id,
                   PublishRequest.clip_index, PublishRequest.content_fingerprint)
            .join(PublishRequest,
                  PublishRequest.id == PublishAttempt.publish_request_id)
            .where(PublishAttempt.status.in_([state.PENDING, state.DEFERRED]))
            .where(PublishRequest.content_fingerprint.is_not(None))
            .order_by(PublishRequest.scheduled_for.asc().nullsfirst(),
                      PublishAttempt.created_at.asc())
            .limit(limit)
        )).all()
    for group_id, job_id, clip_index, fingerprint in rows:
        key = (job_id, clip_index, fingerprint)
        if key in seen:
            continue
        seen.add(key)
        out.append({"group_id": group_id, "job_id": job_id,
                    "clip_index": clip_index, "fingerprint": fingerprint})
    return out


async def _log_transfer(cand: dict, event: str, message: str) -> None:
    try:
        async with db.session() as session:
            async with session.begin():
                await service.log_event(
                    session, event,
                    message=f"{cand['job_id']}[{cand['clip_index']}]: {message}",
                    group_id=cand.get("group_id"))
    except Exception as e:  # pragma: no cover - logging must not break the loop
        print(f"⚠️  Publishing: could not log {event}: {e}")


async def _sweep_store_if_due() -> int:
    """Delete staged objects that nothing needs any more.

    Age alone is not the test, and this is where it would be easy to break
    publishing subtly. Two things pin an object:

      * a queued attempt — a post scheduled a week out keeps its clip staged no
        matter how old the object is;
      * a cached provider media ref — the provider stored our presigned URL at
        registration and re-reads it at post time, so deleting the object under a
        live ref turns a healthy post into "could not download the file".

    What is left is the tail: objects whose posts already went out and whose refs
    have expired.
    """
    global _last_store_sweep
    from . import objectstore, state
    from .models import PublishAttempt, PublishRequest

    hours = settings.store_retention_hours
    if hours <= 0:  # sweeping disabled; the bucket has its own lifecycle rule
        return 0
    now = time.monotonic()
    if _last_store_sweep and now - _last_store_sweep < STORE_SWEEP_INTERVAL_SECONDS:
        return 0
    _last_store_sweep = now

    try:
        objects = await asyncio.to_thread(objectstore.list_objects)
    except objectstore.StoreError as e:
        print(f"⚠️  Publishing: media store sweep could not list objects: {e}")
        return 0
    if not objects:
        return 0

    cutoff = _now() - timedelta(hours=hours)
    stale = []
    for item in objects:
        modified = item.get("last_modified")
        if modified is None:
            continue
        if modified.tzinfo is None:  # pragma: no cover - botocore sends tz-aware
            modified = modified.replace(tzinfo=timezone.utc)
        if modified <= cutoff:
            stale.append(item["key"])
    if not stale:
        return 0

    wanted = {objectstore.key_fingerprint(k) for k in stale}
    wanted.discard(None)
    if not wanted:
        return 0
    async with db.session() as session:
        pinned = set((await session.execute(
            select(PublishRequest.content_fingerprint)
            .join(PublishAttempt,
                  PublishAttempt.publish_request_id == PublishRequest.id)
            .where(PublishRequest.content_fingerprint.in_(wanted))
            .where(PublishAttempt.status.in_([
                state.PENDING, state.DEFERRED, state.IN_FLIGHT,
                state.SUBMITTED]))
        )).scalars().all())
        pinned |= set((await session.execute(
            select(PublishMedia.content_fingerprint)
            .where(PublishMedia.content_fingerprint.in_(wanted))
        )).scalars().all())

    deleted = 0
    for key in stale:
        if objectstore.key_fingerprint(key) in pinned:
            continue
        try:
            await asyncio.to_thread(objectstore.delete, key)
            deleted += 1
        except objectstore.StoreError as e:
            print(f"⚠️  Publishing: could not delete staged object: {e}")
            break
    if deleted:
        print(f"🧹 Publishing: removed {deleted} staged clip(s) from the media "
              f"store (older than {hours:g}h, nothing queued needs them).")
    return deleted
