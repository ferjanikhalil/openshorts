"""The scheduled entrypoint, and the two guards that make a shared queue safe.

``publishing/tick.py`` runs one pass and exits, so a host that only offers
*scheduled* compute can hold the publish clock. That changes an assumption the
queue was built on. With one long-lived process, "boot" meant nothing was in
flight and boot recovery could re-queue every ``in_flight`` row it found. A tick
boots every ten minutes, overlaps its predecessor when a pass runs long, and
shares the table with the app host — so that same recovery pass now regularly
runs while another process is mid-batch.

Re-queuing a claim someone is still working through is how one clip becomes two
posts, and a duplicate post to a real audience is the one mistake in this
subsystem with no undo. Two things prevent it, and both are tested here:

  * ``dispatcher.dispatch_attempt`` re-checks that the claim is still ours
    before it submits anything — the actual boundary;
  * ``service.recover_orphaned_claims`` only takes claims that have gone quiet
    long enough to be abandoned rather than merely slow — thrash avoidance.

The rest is the tick's own assembly: what it refuses to run without, and the
order of the pass, which decides whether a slot that came due goes out now or in
another interval.

No database, no network, no credentials.
"""
import asyncio
import importlib
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from publishing import dispatcher, service, state, tick
from publishing.config import ORPHAN_CLAIM_MIN_AGE_SECONDS
from publishing.providers.status200 import CONNECT_TIMEOUT, READ_TIMEOUT

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Valid base64 for 32 bytes — validate_required decodes it.
_MASTER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_MIN_ENV = {
    "PUBLISHING_ENABLED": "1",
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5432/none",
    "PUBLISHING_MASTER_KEY": _MASTER_KEY,
}

# The longest a single legitimate dispatch can take: one media registration and
# one submit, each bounded by the provider client's own timeouts. Anything that
# claims to know when a claim is abandoned, or when a pass has run long enough,
# has to be larger than this or it fights real work.
WORST_CASE_DISPATCH = 2 * (CONNECT_TIMEOUT + READ_TIMEOUT)


def _utc():
    return datetime.now(timezone.utc)


@pytest.fixture
def min_env(monkeypatch):
    for key, value in _MIN_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PUBLISHING_TICK_BUDGET_SECONDS", raising=False)
    return monkeypatch


# --- The claim age rule -----------------------------------------------------
class TestOrphanRecoveryIsAgeBounded:
    """``claim_is_recoverable``: may a second worker take this claim?

    Pure on purpose — it is the rule that hands a row a live worker may still be
    holding to somebody else, so it must be checkable without Postgres.
    """

    def test_a_fresh_claim_is_left_alone(self):
        now = _utc()
        assert not service.claim_is_recoverable(now, now, 900)
        assert not service.claim_is_recoverable(
            now - timedelta(seconds=60), now, 900)

    def test_a_claim_quiet_long_enough_is_recoverable(self):
        now = _utc()
        assert service.claim_is_recoverable(
            now - timedelta(seconds=901), now, 900)

    def test_the_boundary_is_inclusive(self):
        now = _utc()
        assert service.claim_is_recoverable(
            now - timedelta(seconds=900), now, 900)

    def test_a_claim_with_no_timestamp_is_recoverable_immediately(self):
        # Every real claim stamps claimed_at, so NULL means nothing holds this
        # row — waiting 15 minutes to re-queue it would delay a post for nothing.
        assert service.claim_is_recoverable(None, _utc(), 900)

    def test_a_naive_timestamp_is_read_as_utc(self):
        # Defensive: a column read through a driver that drops tzinfo must not
        # raise here, because the caller is a recovery pass that swallows.
        now = _utc()
        naive = (now - timedelta(seconds=1000)).replace(tzinfo=None)
        assert service.claim_is_recoverable(naive, now, 900)

    def test_the_default_outlasts_a_real_dispatch(self):
        # If the bound were shorter than the work, recovery would re-queue rows
        # that are simply slow — and the queue would spend its time fighting
        # itself instead of publishing.
        assert ORPHAN_CLAIM_MIN_AGE_SECONDS > WORST_CASE_DISPATCH


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RowsSession:
    """Just enough session for recover_orphaned_claims: one SELECT, N rows."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt):
        return _ScalarRows(self.rows)


def _in_flight(age_seconds, owner="worker-a"):
    return SimpleNamespace(
        status=state.IN_FLIGHT, claimed_by=owner,
        claimed_at=_utc() - timedelta(seconds=age_seconds))


class TestRecoveryAppliesTheRule:
    def test_it_skips_a_claim_another_worker_is_still_working(self, min_env):
        row = _in_flight(30)
        session = _RowsSession([row])
        assert asyncio.run(service.recover_orphaned_claims(session)) == 0
        assert row.status == state.IN_FLIGHT
        assert row.claimed_by == "worker-a", "the claim must stay attributed"

    def test_it_requeues_an_abandoned_claim(self, min_env):
        row = _in_flight(ORPHAN_CLAIM_MIN_AGE_SECONDS + 60)
        session = _RowsSession([row])
        assert asyncio.run(service.recover_orphaned_claims(session)) == 1
        assert row.status == state.PENDING
        assert row.claimed_at is None and row.claimed_by is None

    def test_a_mixed_batch_recovers_only_the_quiet_rows(self, min_env):
        fresh = _in_flight(5)
        stale = _in_flight(ORPHAN_CLAIM_MIN_AGE_SECONDS + 1)
        session = _RowsSession([fresh, stale])
        assert asyncio.run(service.recover_orphaned_claims(session)) == 1
        assert fresh.status == state.IN_FLIGHT
        assert stale.status == state.PENDING

    def test_the_bound_is_configurable_per_call(self, min_env):
        row = _in_flight(120)
        session = _RowsSession([row])
        assert asyncio.run(
            service.recover_orphaned_claims(session, min_age_seconds=60)) == 1

    def test_the_env_override_is_honoured(self, min_env):
        min_env.setenv("PUBLISHING_ORPHAN_CLAIM_MIN_AGE", "60")
        row = _in_flight(120)
        session = _RowsSession([row])
        assert asyncio.run(service.recover_orphaned_claims(session)) == 1


# --- The claim ownership guard ----------------------------------------------
class _GetSession:
    """Records the lookups dispatch makes, and answers every one with None."""

    def __init__(self):
        self.gets = 0

    async def execute(self, stmt):  # pragma: no cover - not reached here
        raise AssertionError("dispatch queried the database after claim_lost")

    async def get(self, model, pk):
        self.gets += 1
        return None


def _attempt(**over):
    values = {"id": "att-1", "status": state.IN_FLIGHT, "claimed_by": "worker-a",
              "claimed_at": _utc(), "publish_destination_id": "dest-1",
              "publish_request_id": "req-1", "publish_group_id": "grp-1"}
    values.update(over)
    return SimpleNamespace(**values)


class TestClaimOwnershipGuard:
    """The boundary that makes an early recovery wasteful instead of destructive.

    The sequence this defends against, all of it ordinary operation once two
    processes share the queue: tick A claims a batch and works it serially; the
    webhook container cold-starts and runs boot recovery; it finds a tail row of
    A's batch still ``in_flight``, re-queues it, claims it, submits it. Tick A
    then reaches that same row — and without this check submits it again.

    The partial unique index does not cover it: this is one row submitted twice,
    not two rows. ``SKIP LOCKED`` does not either — the re-read in
    ``worker.dispatch_once`` is a ``session.get``, which takes no row lock.
    """

    def test_a_claim_taken_over_by_another_worker_stops_dead(self):
        attempt = _attempt(claimed_by="worker-b")
        session = _GetSession()
        result = asyncio.run(
            dispatcher.dispatch_attempt(session, attempt, claim_owner="worker-a"))
        assert result == "claim_lost"
        assert session.gets == 0, "nothing may be read once the claim is gone"

    def test_a_requeued_row_stops_dead(self):
        # Recovery re-queued it and nobody has picked it up yet: status alone is
        # enough to know this claim is void.
        attempt = _attempt(status=state.PENDING, claimed_by=None)
        session = _GetSession()
        assert asyncio.run(dispatcher.dispatch_attempt(
            session, attempt, claim_owner="worker-a")) == "claim_lost"

    def test_a_row_already_resolved_elsewhere_stops_dead(self):
        attempt = _attempt(status=state.SUCCEEDED, claimed_by="worker-a")
        session = _GetSession()
        assert asyncio.run(dispatcher.dispatch_attempt(
            session, attempt, claim_owner="worker-a")) == "claim_lost"

    def test_the_guard_runs_before_anything_reaches_a_provider(self,
                                                              monkeypatch):
        def boom(*a, **kw):  # pragma: no cover - must never be called
            raise AssertionError("claim_lost still touched the provider registry")

        monkeypatch.setattr(dispatcher.providers, "get", boom)
        assert asyncio.run(dispatcher.dispatch_attempt(
            _GetSession(), _attempt(claimed_by="worker-b"),
            claim_owner="worker-a")) == "claim_lost"

    def test_our_own_claim_proceeds(self, monkeypatch):
        # The guard must not become a wall: a row we hold goes on to the normal
        # checks. Destination lookup answers None here, which is the first of
        # them, so reaching "no_destination" proves check 0 let it through.
        recorded = []

        async def record_failure(session, attempt, err):
            recorded.append(err)

        monkeypatch.setattr(dispatcher.service, "record_failure", record_failure)
        session = _GetSession()
        result = asyncio.run(dispatcher.dispatch_attempt(
            session, _attempt(), claim_owner="worker-a"))
        assert result == "no_destination"
        assert session.gets == 1 and len(recorded) == 1

    def test_omitting_the_owner_skips_the_check(self, monkeypatch):
        # Backwards compatibility for callers that claim and dispatch inside one
        # transaction, where no other process can have intervened.
        async def record_failure(session, attempt, err):
            pass

        monkeypatch.setattr(dispatcher.service, "record_failure", record_failure)
        session = _GetSession()
        result = asyncio.run(dispatcher.dispatch_attempt(
            session, _attempt(claimed_by="somebody-else")))
        assert result == "no_destination"


# --- What the tick refuses to run without -----------------------------------
class TestTickRefusals:
    """A cron job that exits 0 having done nothing is indistinguishable, in
    every dashboard a scheduled host offers, from one that had nothing to do."""

    def test_publishing_disabled_is_a_hard_error(self, min_env):
        min_env.delenv("PUBLISHING_ENABLED", raising=False)
        with pytest.raises(RuntimeError, match="PUBLISHING_ENABLED"):
            tick.require_config()

    def test_a_missing_master_key_is_a_hard_error(self, min_env):
        min_env.delenv("PUBLISHING_MASTER_KEY", raising=False)
        with pytest.raises(RuntimeError, match="PUBLISHING_MASTER_KEY"):
            tick.require_config()

    def test_a_missing_database_url_is_a_hard_error(self, min_env):
        min_env.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            tick.require_config()

    def test_a_valid_config_passes(self, min_env):
        tick.require_config()


class TestBudgetConfig:
    def test_the_default_outlasts_a_real_dispatch(self, min_env):
        # A budget shorter than one dispatch would truncate every pass mid-post.
        assert tick._budget_seconds() == tick.DEFAULT_BUDGET_SECONDS
        assert tick.DEFAULT_BUDGET_SECONDS > WORST_CASE_DISPATCH

    def test_the_env_override_is_honoured(self, min_env):
        min_env.setenv("PUBLISHING_TICK_BUDGET_SECONDS", "120")
        assert tick._budget_seconds() == 120


# --- The pass itself --------------------------------------------------------
@pytest.fixture
def fake_worker(min_env, monkeypatch):
    """Replace the DB and the three worker seams; record the order of the pass."""
    from publishing import db, worker

    calls = []
    plan = {"dispatch": [0], "recovered": 0, "staged": 0,
            "reconciled": {"webhooks": 0, "stale": 0, "assignments": 0,
                           "slots": 0, "media_preregistered": 0}}
    limits = []

    async def init():
        calls.append("init")

    async def recover():
        calls.append("recover")
        return plan["recovered"]

    async def reconcile():
        calls.append("reconcile")
        return plan["reconciled"]

    async def dispatch_once(limit=10):
        calls.append("dispatch")
        limits.append(limit)
        queue = plan["dispatch"]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def transfer():
        calls.append("transfer")
        return plan["staged"]

    monkeypatch.setattr(db, "init", init)
    monkeypatch.setattr(worker, "recover_stale_on_boot", recover)
    monkeypatch.setattr(worker, "reconcile_once", reconcile)
    monkeypatch.setattr(worker, "dispatch_once", dispatch_once)
    monkeypatch.setattr(worker, "transfer_once", transfer)
    return SimpleNamespace(calls=calls, plan=plan, limits=limits)


class TestTheOrderOfThePass:
    def test_reconcile_happens_before_dispatch(self, fake_worker):
        # The reason the tick exists. Reconciliation is what turns a due
        # assignment into a publish request, so dispatching first would add a
        # whole tick interval to every scheduled post.
        asyncio.run(tick.run_once())
        assert fake_worker.calls.index("reconcile") < \
            fake_worker.calls.index("dispatch")

    def test_recovery_happens_before_reconciling(self, fake_worker):
        asyncio.run(tick.run_once())
        assert fake_worker.calls[:3] == ["init", "recover", "reconcile"]

    def test_the_store_sweep_runs_last_and_always(self, fake_worker):
        # It moves nothing on a clip-less host; it is called for the retention
        # sweep it drives, which is the only sweeper running while the app host
        # is asleep.
        asyncio.run(tick.run_once())
        assert fake_worker.calls[-1] == "transfer"


class TestTheDrainLoop:
    def test_it_keeps_going_until_the_queue_is_empty(self, fake_worker):
        fake_worker.plan["dispatch"] = [1, 1, 1, 0]
        out = asyncio.run(tick.run_once())
        assert out["dispatched"] == 3
        assert out["truncated"] is False
        assert fake_worker.calls.count("dispatch") == 4, "one idle pass to stop"

    def test_it_claims_one_attempt_at_a_time(self, fake_worker):
        # A claimed batch is worked serially, so a batch of ten leaves the last
        # row in_flight for minutes. One at a time means a claim lives exactly as
        # long as its own dispatch — which for a process the host kills on a
        # schedule is the difference between a stranded post and none.
        fake_worker.plan["dispatch"] = [1, 0]
        asyncio.run(tick.run_once())
        assert set(fake_worker.limits) == {1}

    def test_an_idle_tick_dispatches_nothing(self, fake_worker):
        out = asyncio.run(tick.run_once())
        assert out["dispatched"] == 0 and out["truncated"] is False

    def test_the_runaway_cap_truncates(self, fake_worker, monkeypatch):
        monkeypatch.setattr(tick, "MAX_DISPATCHES_PER_TICK", 3)
        fake_worker.plan["dispatch"] = [1]  # never empties
        out = asyncio.run(tick.run_once())
        assert out["dispatched"] == 3 and out["truncated"] is True

    def test_the_budget_truncates_and_still_sweeps(self, fake_worker, min_env):
        # Negative rather than 0 so the first check trips deterministically,
        # without depending on how long an awaited stub takes.
        min_env.setenv("PUBLISHING_TICK_BUDGET_SECONDS", "-1")
        fake_worker.plan["dispatch"] = [1]
        out = asyncio.run(tick.run_once())
        assert out["truncated"] is True
        assert out["dispatched"] == 0
        assert "dispatch" not in fake_worker.calls
        # Leaving the queue for the next tick is the intended outcome; being
        # killed by the host mid-claim is what it avoids.
        assert fake_worker.calls[-1] == "transfer"

    def test_it_reports_what_happened(self, fake_worker):
        fake_worker.plan["recovered"] = 2
        fake_worker.plan["staged"] = 1
        fake_worker.plan["reconciled"] = {"webhooks": 4, "slots": 3,
                                          "assignments": 2, "stale": 1,
                                          "media_preregistered": 1}
        fake_worker.plan["dispatch"] = [1, 1, 0]
        out = asyncio.run(tick.run_once())
        assert out["recovered"] == 2
        assert out["staged"] == 1
        assert out["dispatched"] == 2
        assert out["reconciled"]["webhooks"] == 4
        assert isinstance(out["seconds"], float)

    def test_a_disabled_tick_never_touches_the_database(self, fake_worker,
                                                       min_env):
        min_env.delenv("PUBLISHING_ENABLED", raising=False)
        with pytest.raises(RuntimeError):
            asyncio.run(tick.run_once())
        assert fake_worker.calls == []


class TestSummary:
    def test_it_carries_every_count_a_human_scans_for(self):
        line = tick.summarize({
            "recovered": 2, "dispatched": 5, "staged": 1, "seconds": 12.3,
            "reconciled": {"slots": 3, "assignments": 2, "webhooks": 1,
                           "stale": 0, "media_preregistered": 4}})
        for fragment in ("dispatched=5", "slots=3", "requests=2", "webhooks=1",
                         "stale=0", "media=4", "recovered=2", "in=12.3s"):
            assert fragment in line

    def test_truncation_is_visible(self):
        assert "budget" in tick.summarize({"truncated": True})

    def test_an_empty_result_still_renders(self):
        # summarize runs after a pass that may have returned early; it must not
        # be the thing that raises in a cron log.
        assert "dispatched=0" in tick.summarize({})


class TestImportIsolation:
    """A subprocess, because in-process checks cannot prove absence.

    Another test in this session may already have imported torch, so
    ``"torch" in sys.modules`` would answer the wrong question here.
    """

    def test_a_tick_pulls_in_no_video_pipeline(self):
        heavy = ("app", "main", "torch", "ultralytics", "mediapipe",
                 "faster_whisper", "cv2", "yt_dlp")
        # The deferred imports are forced explicitly. ``run_once`` imports db and
        # worker inside the function body, so importing the module alone would
        # pass this test while proving nothing about what a real pass loads.
        code = (
            "import sys; import publishing.tick; "
            "from publishing import db, worker; "
            f"print('<<<' + ','.join(m for m in {heavy!r} if m in sys.modules)"
            " + '>>>')"
        )
        env = dict(os.environ)
        env.update(_MIN_ENV)
        # Emoji in the boot lines: the child writes UTF-8, so decode as UTF-8.
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        assert proc.returncode == 0, proc.stderr[-2000:]
        marked = [line for line in proc.stdout.splitlines()
                  if line.startswith("<<<") and line.endswith(">>>")]
        assert len(marked) == 1, f"no sentinel in output: {proc.stdout[-500:]}"
        leaked = marked[0][3:-3]
        assert not leaked, (
            f"a publishing tick imported {leaked}. The scheduled host installs "
            "none of that — see deploy/publisher/requirements.txt — so this "
            "import would crash every tick at startup.")

    def test_the_module_is_runnable_as_a_script(self):
        # `python -m publishing.tick` is what the scheduled host invokes, and a
        # missing __main__ guard or a bad import would only show up there.
        proc = subprocess.run(
            [sys.executable, "-m", "publishing.tick"], cwd=REPO_ROOT,
            env={**os.environ, "PUBLISHING_ENABLED": ""},
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        # Disabled, so it must fail loudly rather than exit 0 having done nothing.
        assert proc.returncode != 0
        assert "PUBLISHING_ENABLED" in (proc.stderr + proc.stdout)


def test_the_module_reloads_cleanly():
    # tick holds no import-time state; a scheduled host may import it per pass.
    importlib.reload(tick)
