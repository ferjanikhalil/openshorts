"""The one coroutine ``app.py`` calls when a job's clips are finally ready.

Kept separate from ``planner`` because the caller is unusual: the autopilot
styling worker is a daemon *thread*, hours after the request that started it, so
this has to own its own DB session and swallow its own errors. A publishing
failure must never mark a finished, styled job as failed — the clips exist and
are downloadable either way.

``app.py`` holds the only reference to this module's public function, which is
what keeps the dependency one-directional: publishing never imports ``app``.
"""
from . import db, planner, service


async def publish_job_clips(job_id: str, clip_count: int, plan: dict,
                            user_id=None, actor: str = "autopilot") -> dict:
    """Plan and enqueue a finished job's clips. Never raises.

    Returns the planner's report so the caller can log a line the operator can
    act on ("3 scheduled, 1 skipped: clip unavailable") rather than a bare
    success/failure.
    """
    try:
        async with db.session() as session:
            async with session.begin():
                report = await planner.plan_job(
                    session, job_id=job_id, clip_count=clip_count, plan=plan,
                    user_id=user_id, actor=actor)
        return report
    except Exception as e:
        # A thread-scheduled coroutine has nowhere to propagate to, and the job
        # itself succeeded. Log loudly, report the failure, stay out of the way.
        message = _scrubbed(e)
        print(f"⚠️  Publishing: auto-publish failed for job {job_id}: {message}")
        try:
            async with db.session() as session:
                async with session.begin():
                    await service.log_event(
                        session, "autopublish.failed", message=message[:500],
                        actor=actor, data={"job_id": job_id})
        except Exception:
            # The DB is the thing that just failed; there is nowhere left to
            # write. The stdout line above is the record.
            pass
        return {"ok": False, "created": [], "skipped": [], "reason": message}


def _scrubbed(exc: Exception) -> str:
    from . import crypto
    return crypto.scrub(f"{type(exc).__name__}: {exc}")
