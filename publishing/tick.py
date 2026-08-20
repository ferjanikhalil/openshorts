"""One publishing pass, then exit: the entrypoint for a scheduled host.

Why a second entrypoint
-----------------------
``runner.py`` is the same subsystem as a long-lived process. This is the same
subsystem as a *cron job*. Both exist for one reason: nothing in the chain will
hold a schedule for us. Status 200 accepts a ``scheduledFor`` field and discards
it (measured over three real posts — see ``providers/status200.py``), and
TikTok's and Instagram's APIs expose no scheduled publish at all. So a clock has
to sit between "the plan says 18:00" and "submit now", and on a single-machine
deploy that clock is the laptop's: a slot fires only if the laptop happens to be
awake at it.

A process that runs for a minute every ten minutes can hold that clock just as
well as one that never exits, and free scheduled compute is far easier to come by
than a free always-on VM. What makes it work at all is that a submit needs no
bytes: the provider fetches the clip from the staging bucket by presigned URL, so
this process needs Postgres, the master key and outbound HTTPS. Nothing else, and
nothing local.

The cost is granularity. A slot is honoured at the first tick after it, so the
tick interval is the worst-case lateness — ten minutes on a ten-minute cron. For
"post three clips a day on a rhythm" that is invisible; for anything wanting
to-the-second timing it is not the right tool.

The order of the pass
---------------------
Reconcile, then dispatch. Reconciliation is what turns a due *assignment* into a
publish request, so doing it first means a slot that came due since the last tick
goes out in *this* tick. Dispatching first would add a full tick interval to
every scheduled post, which is the one thing this file exists to avoid.

Draining, not one batch
-----------------------
``worker.dispatch_once`` handles one claimed batch. A tick has to keep going
until the queue is empty, because the next chance is a whole interval away — so
it loops, bounded by a wall-clock budget and a runaway cap.

It claims ONE attempt per batch rather than the default ten. ``dispatch_once``
works a claimed batch serially, so with a batch of ten the last row sits
``in_flight`` for as long as the nine before it take — minutes, during which a
crash strands it. One at a time means a claim lives exactly as long as its own
dispatch, which for a process that is killed and restarted on a schedule is the
difference between a stranded post and none.

Run:
    python -m publishing.tick

Deployed on Modal by ``deploy/publisher/modal_app.py``; see
``deploy/publisher/README.md``.
"""
import asyncio
import os
import time

import publishing

# Wall-clock budget for one pass. The scheduled host imposes its own timeout and
# kills the process when it expires; a claim interrupted that way is stranded
# until recovery ages it out (config.ORPHAN_CLAIM_MIN_AGE_SECONDS). So the tick
# stops on its own terms first, and leaves the rest for the next one — the queue
# is durable, and a post going out ten minutes later beats one going out after a
# recovery delay. Keep this comfortably below the host's function timeout.
DEFAULT_BUDGET_SECONDS = 600

# Runaway guard, not a throttle: the budget above is what normally ends the
# drain. This only matters if attempts start resolving instantly in a cycle.
MAX_DISPATCHES_PER_TICK = 200


def _budget_seconds() -> int:
    return int(os.environ.get("PUBLISHING_TICK_BUDGET_SECONDS",
                              str(DEFAULT_BUDGET_SECONDS)))


def require_config() -> None:
    """Refuse to run in a shape that would publish nothing and report success.

    This matters more here than in a long-lived process. A cron job that exits 0
    having done nothing looks identical, in every dashboard the host offers, to
    one that had nothing to do — so a misconfigured tick would report green
    forever while every scheduled post silently missed its slot, which is exactly
    the symptom this deployment exists to fix.
    """
    if not publishing.is_enabled():
        raise RuntimeError(
            "PUBLISHING_ENABLED is not set, so this tick would do nothing and "
            "exit successfully. Set PUBLISHING_ENABLED=1 in the scheduled "
            "host's secret, or do not schedule this function."
        )
    # DATABASE_URL + a valid master key. Without the key the credential cannot be
    # unwrapped and every submit fails looking exactly like a revoked provider
    # key — a failure worth having at boot instead of at the slot.
    publishing.validate_required()


async def run_once() -> dict:
    """Reconcile, drain the due queue, sweep. Returns what happened.

    The return value is the tick's only output that a machine can read, so it
    carries counts rather than prose; the printed line is for a human reading the
    host's logs.
    """
    from . import db, worker

    require_config()

    started = time.monotonic()
    budget = _budget_seconds()
    out = {"recovered": 0, "reconciled": {}, "dispatched": 0, "staged": 0,
           "truncated": False, "seconds": 0.0}

    await db.init()

    # Claims stranded by a previous tick that was killed mid-dispatch. Age-bounded
    # inside `service.recover_orphaned_claims`, which is load-bearing here: ticks
    # overlap with each other and with the app host, and re-queuing a claim
    # another process is still working through is how one clip becomes two posts.
    out["recovered"] = await worker.recover_stale_on_boot()

    out["reconciled"] = await worker.reconcile_once()

    while True:
        if time.monotonic() - started > budget:
            out["truncated"] = True
            break
        if out["dispatched"] >= MAX_DISPATCHES_PER_TICK:
            out["truncated"] = True
            break
        handled = await worker.dispatch_once(limit=1)
        if not handled:
            break
        out["dispatched"] += handled

    # On a host with no clip files this moves nothing — `clips.resolve` answers
    # for nothing, so every candidate is skipped without a record (see
    # `worker.transfer_once`). It is called for the retention sweep it drives,
    # which is what keeps a free-tier bucket's tail from filling up. The sweep's
    # own "not more than every 30 minutes" gate is process-local, so a fresh
    # process sweeps on every tick; that is a list call and a few deletes, and it
    # is the only sweeper that runs at all while the app host is asleep.
    out["staged"] = await worker.transfer_once()

    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def summarize(result: dict) -> str:
    """One log line. Read by a human scanning a scheduled host's run history."""
    rec = result.get("reconciled") or {}
    parts = [
        f"dispatched={result.get('dispatched', 0)}",
        f"slots={rec.get('slots', 0)}",
        f"requests={rec.get('assignments', 0)}",
        f"webhooks={rec.get('webhooks', 0)}",
        f"stale={rec.get('stale', 0)}",
        f"media={rec.get('media_preregistered', 0)}",
        f"recovered={result.get('recovered', 0)}",
        f"in={result.get('seconds', 0)}s",
    ]
    line = f"📡 Publishing tick: {' '.join(parts)}"
    if result.get("truncated"):
        line += " (budget reached — the next tick continues)"
    return line


def main() -> int:
    publishing.make_stdio_utf8_safe()
    result = asyncio.run(run_once())
    print(summarize(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
