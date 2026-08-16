"""Background loops.

Two loops, matching the four ``while True: await asyncio.sleep()`` loops the app
already runs — no broker, no Celery, no Redis. The queue is a table and
``FOR UPDATE SKIP LOCKED`` is the claim, which is the same mechanism
``cloud/metering.py`` already relies on.

  dispatcher  claims due attempts and publishes them
  reconciler  drains webhooks, ages out stale submissions, refreshes media refs,
              and turns due assignments into requests

The scheduler shares the reconciler's tick instead of getting a third loop: an
assignment's granularity is minutes, so a 60-second pass is already far finer
than the thing it schedules, and one fewer loop is one fewer thing to keep
alive.

Each loop swallows its own exceptions. A loop that dies takes publishing down
silently until the next redeploy, which is a worse failure than a logged error.
"""
import asyncio
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import db, dispatcher, planner, service, webhooks
from .config import MEDIA_REFRESH_MARGIN_SECONDS, settings
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


async def recover_stale_on_boot() -> None:
    """Clean up what a previous process left mid-flight.

    Only IN_FLIGHT rows are re-queued. SUBMITTED rows are deliberately left
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
    except Exception as e:
        print(f"⚠️  Publishing: boot recovery failed: {e}")


def start_loops() -> None:
    if _tasks:
        return
    _tasks.append(asyncio.create_task(_dispatch_loop()))
    _tasks.append(asyncio.create_task(_reconcile_loop()))


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
    async with db.session() as session:
        async with session.begin():
            attempts = await service.claim_due_attempts(
                session, worker_id(), limit=limit)
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
                    await dispatcher.dispatch_attempt(session, attempt)
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
    out = {"webhooks": 0, "stale": 0, "media_expired": 0, "assignments": 0}

    out["webhooks"] = await webhooks.drain_pending()

    async with db.session() as session:
        async with session.begin():
            out["stale"] = await service.sweep_stale_submitted(session)
            out["media_expired"] = await _purge_expired_media(session)

    # Its own transaction: creating requests is the one thing in this pass that
    # writes new work, and it must not be rolled back by a sweeper error.
    try:
        async with db.session() as session:
            async with session.begin():
                out["assignments"] = await planner.run_due_assignments(session)
    except Exception as e:
        print(f"⚠️  Publishing scheduler error: {e}")
    return out


async def _purge_expired_media(session) -> int:
    """Drop provider media refs at or past expiry.

    Deleting the row is the whole fix: the next dispatch finds no cache entry and
    re-uploads. Keeping a dead ref would make a scheduled post fail with an
    unfetchable-media error days after the clip was fine.
    """
    cutoff = _now() + timedelta(seconds=MEDIA_REFRESH_MARGIN_SECONDS)
    rows = (await session.execute(
        select(PublishMedia).where(PublishMedia.expires_at.is_not(None),
                                   PublishMedia.expires_at <= cutoff)
    )).scalars().all()
    for row in rows:
        await session.delete(row)
    return len(rows)
