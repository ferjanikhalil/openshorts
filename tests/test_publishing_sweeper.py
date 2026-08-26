"""When a silent submitted post may be called ``unknown`` — and when it may not.

``unknown`` is terminal and never auto-retried, so this sweeper is the one pass
that can end a clip's life on no evidence at all. It fires on *silence*, and
silence has two innocent explanations that both look identical to a lost webhook:

  * ``deferred_until`` — the provider asked for a later window (a daily-cap 202).
  * ``scheduled_for`` — the provider agreed to HOLD the post until a chosen time.
    This is remote-schedule hand-over, and it is the case a ``deferred_until``
    reading cannot cover: ``dispatcher.promote_remote_schedules`` clears
    ``deferred_until`` on purpose, because once the provider owns the clock our
    own hold is meaningless. The submit then goes out immediately carrying
    ``scheduledFor`` and sits quietly in ``submitted`` until the slot arrives.

Against a 30-minute timeout and rhythm slots hours out, reading only
``deferred_until`` condemned *every* remote-scheduled post — a real Zernio post
verified as scheduled on the provider's own dashboard was 13 minutes from being
stamped ``unknown`` while it was still perfectly on track to publish. Nothing
revisits ``unknown``, so the ending was a stranded clip plus a false audit line
saying the provider went quiet.

The suite also pins the other direction, which is the reason the sweeper exists:
a post that genuinely went silent must still age out, or it holds the live-attempt
slot forever and the destination can never be retried.

Stub session, no Postgres, no network — runs in CI with the other pure suites.
The SQL itself is compiled against the real Postgres dialect (``TestTheQuery``),
since a stub cannot fail on a malformed ``FOR UPDATE OF``.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql  # noqa: E402

from publishing import service, state  # noqa: E402
from publishing.models import PublishAttempt  # noqa: E402

NOW = datetime(2026, 8, 25, 14, 0, 0, tzinfo=timezone.utc)
TIMEOUT = 1800                      # PUBLISHING_SUBMIT_TIMEOUT default

GROUP = "7c9f1e2a-0000-4000-8000-0000000000aa"
REQUEST = "7c9f1e2a-0000-4000-8000-0000000000bb"
DEST = "7c9f1e2a-0000-4000-8000-0000000000cc"
ATTEMPT = "7c9f1e2a-0000-4000-8000-0000000000dd"


def run(coro):
    return asyncio.run(coro)


# --- The pure rule ----------------------------------------------------------
def silent(**over):
    """Arguments for a post that has been quiet well past the timeout."""
    args = {
        "submitted_at": NOW - timedelta(seconds=TIMEOUT * 2),
        "deferred_until": None,
        "scheduled_for": None,
        "timeout_seconds": TIMEOUT,
    }
    args.update(over)
    return args


class TestConfirmationIsOverdue:
    def test_a_long_silent_post_is_overdue(self):
        assert state.confirmation_is_overdue(NOW, **silent()) is True

    def test_a_fresh_submit_is_not(self):
        assert state.confirmation_is_overdue(
            NOW, **silent(submitted_at=NOW - timedelta(seconds=TIMEOUT - 1))) \
            is False

    def test_the_timeout_is_inclusive(self):
        assert state.confirmation_is_overdue(
            NOW, **silent(submitted_at=NOW - timedelta(seconds=TIMEOUT))) is True

    def test_a_provider_parked_window_is_left_alone(self):
        """A future `deferred_until` is the provider's own window (a daily-cap
        202): silence before it is expected, not suspicious."""
        assert state.confirmation_is_overdue(
            NOW, **silent(deferred_until=NOW + timedelta(hours=2))) is False

    def test_a_remote_schedule_slot_is_left_alone(self):
        """The bug, stated as one assertion.

        The post was submitted hours ago and is deliberately silent because the
        PROVIDER is holding it until its slot. `deferred_until` is None here
        because hand-over cleared it — which is exactly why it cannot be the only
        field consulted.
        """
        assert state.confirmation_is_overdue(
            NOW, **silent(scheduled_for=NOW + timedelta(minutes=13))) is False

    def test_a_slot_hours_out_is_still_left_alone(self):
        # A rhythm slot is typically hours away and the timeout is 30 minutes, so
        # the gap the old reading fell into is the normal case, not an edge one.
        assert state.confirmation_is_overdue(
            NOW, **silent(scheduled_for=NOW + timedelta(hours=9))) is False

    def test_the_timeout_runs_from_the_slot_not_the_submit(self):
        """After the slot passes the post owes us a confirmation, but only after
        the usual grace — the provider needs time to publish and call back."""
        past_slot = NOW - timedelta(seconds=TIMEOUT - 60)
        assert state.confirmation_is_overdue(
            NOW, **silent(scheduled_for=past_slot)) is False
        assert state.confirmation_is_overdue(
            NOW, **silent(scheduled_for=NOW - timedelta(seconds=TIMEOUT))) is True

    def test_the_latest_hold_wins(self):
        """Both fields can be set; the post is held until the last of them."""
        assert state.confirmation_is_overdue(
            NOW, **silent(deferred_until=NOW - timedelta(hours=1),
                          scheduled_for=NOW + timedelta(minutes=5))) is False
        assert state.confirmation_is_overdue(
            NOW, **silent(deferred_until=NOW + timedelta(minutes=5),
                          scheduled_for=NOW - timedelta(hours=1))) is False

    def test_a_past_slot_does_not_shorten_the_submit_timeout(self):
        """A slot already gone must not make a fresh submit look overdue.

        Catch-up conversion submits with `scheduled_for` in the past; taking the
        max of the two rather than the schedule alone is what keeps that post's
        grace period intact.
        """
        assert state.confirmation_is_overdue(
            NOW, **silent(submitted_at=NOW - timedelta(seconds=60),
                          scheduled_for=NOW - timedelta(days=1))) is False

    def test_a_post_never_submitted_is_never_overdue(self):
        assert state.confirmation_is_overdue(NOW, **silent(submitted_at=None)) \
            is False

    def test_naive_timestamps_do_not_raise(self):
        """This runs inside reconciliation: a TypeError on one row would stop
        every other post from being polled, swept or scheduled that tick."""
        assert state.confirmation_is_overdue(
            NOW, **silent(submitted_at=datetime(2026, 8, 25, 12, 0, 0),
                          scheduled_for=datetime(2026, 8, 25, 12, 30, 0))) is True

    def test_a_zero_timeout_condemns_immediately(self):
        # The knob bottoms out rather than misbehaving.
        assert state.confirmation_is_overdue(
            NOW, **silent(submitted_at=NOW, timeout_seconds=0)) is True


# --- Stubs ------------------------------------------------------------------
class StubSession:
    """Answers the one query the sweeper makes.

    The real query selects ``(attempt, request.scheduled_for)`` pairs, so the stub
    hands back pairs. It also captures the statement so ``TestTheQuery`` can
    compile it: the pre-filter is SQL that CI cannot execute, and a wrong ``FOR
    UPDATE OF`` or a missing join would only ever surface against live Postgres.
    """

    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = list(self._rows)
        return SimpleNamespace(all=lambda: rows)


def make_attempt(**over):
    row = SimpleNamespace(
        id=ATTEMPT,
        publish_request_id=REQUEST,
        publish_destination_id=DEST,
        publish_group_id=GROUP,
        provider="zernio",
        platform="tiktok",
        status=state.SUBMITTED,
        submitted_at=NOW - timedelta(seconds=TIMEOUT * 2),
        deferred_until=None,
        completed_at=None,
        error_code=None,
        error_message=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


@pytest.fixture
def harness(monkeypatch):
    calls = {"events": [], "refreshed": []}

    async def _log_event(_session, kind, **kw):
        calls["events"].append((kind, kw.get("message", "")))

    async def _refresh(_session, request_id):
        calls["refreshed"].append(request_id)

    monkeypatch.setattr(service, "log_event", _log_event)
    monkeypatch.setattr(service, "refresh_request_status", _refresh)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setenv("PUBLISHING_SUBMIT_TIMEOUT", str(TIMEOUT))
    return SimpleNamespace(calls=calls)


def sweep(attempts, *, scheduled_for=None):
    session = StubSession([(a, scheduled_for) for a in attempts])
    return run(service.sweep_stale_submitted(session)), session


# --- What the sweeper does to a row -----------------------------------------
class TestSweep:
    def test_a_genuinely_silent_post_ages_out(self, harness):
        """The reason the pass exists. Without it the row holds the live-attempt
        slot forever and the destination can never be retried."""
        attempt = make_attempt()
        count, _ = sweep([attempt])

        assert count == 1
        assert attempt.status == state.UNKNOWN
        assert attempt.completed_at == NOW
        assert attempt.error_code == "no_confirmation"
        assert "may or may not be live" in attempt.error_message
        assert ("attempt.unknown", "submit confirmation timeout") \
            in harness.calls["events"]
        assert harness.calls["refreshed"] == [REQUEST]

    def test_a_remote_scheduled_post_is_spared(self, harness):
        """The regression guard, at the level the bug actually bit.

        The row's own columns look exactly like a lost webhook — submitted an
        hour ago, no `deferred_until`, no confirmation. Only the request's
        `scheduled_for` says otherwise.
        """
        attempt = make_attempt()
        count, _ = sweep([attempt], scheduled_for=NOW + timedelta(minutes=13))

        assert count == 0
        assert attempt.status == state.SUBMITTED
        assert attempt.completed_at is None
        assert attempt.error_code is None
        assert harness.calls["events"] == []
        assert harness.calls["refreshed"] == []

    def test_it_is_swept_once_the_slot_and_the_grace_have_passed(self, harness):
        """Sparing must be a delay, not an exemption: a post the provider claimed
        to have scheduled and then never confirmed still has to surface."""
        attempt = make_attempt(submitted_at=NOW - timedelta(days=1))
        count, _ = sweep(
            [attempt], scheduled_for=NOW - timedelta(seconds=TIMEOUT + 60))

        assert count == 1
        assert attempt.status == state.UNKNOWN

    def test_a_provider_parked_window_is_spared(self, harness):
        # Pre-existing behaviour, kept passing through the rewrite.
        attempt = make_attempt(deferred_until=NOW + timedelta(hours=2))
        count, _ = sweep([attempt])
        assert count == 0
        assert attempt.status == state.SUBMITTED

    def test_a_row_the_query_should_not_have_returned_is_re_checked(self, harness):
        """The Python guard is the authority, the SQL is the pre-filter.

        Same contract as `poll_is_due`: drift between them must cost a skipped
        sweep, never a wrongly condemned post — so a too-fresh row handed over by
        the stub query is dropped here.
        """
        attempt = make_attempt(submitted_at=NOW - timedelta(seconds=60))
        count, _ = sweep([attempt])
        assert count == 0
        assert attempt.status == state.SUBMITTED

    def test_the_inner_join_cannot_hide_an_attempt(self, harness):
        """Why the join is safe to make inner rather than outer.

        An inner join drops an attempt whose request is missing — which would
        wedge it in `submitted` forever. It cannot happen: the column is NOT NULL
        and the FK cascades, so an attempt without a request is not a row the
        sweeper should have found, it is a row that no longer exists.
        """
        col = PublishAttempt.__table__.c.publish_request_id
        assert col.nullable is False
        assert [fk.ondelete for fk in col.foreign_keys] == ["CASCADE"]

    def test_a_mixed_batch_is_decided_per_row(self, harness):
        """One spared row must not spare the batch, and vice versa.

        A single pass covers every group and every request, so the schedules in
        one batch differ — the verdict has to be per pair, not per pass.
        """
        silent_row = make_attempt(id="a-silent")
        parked_row = make_attempt(id="a-parked",
                                  deferred_until=NOW + timedelta(hours=2))
        held_row = make_attempt(id="a-held")
        session = StubSession([(silent_row, None), (parked_row, None),
                               (held_row, NOW + timedelta(hours=9))])
        count = run(service.sweep_stale_submitted(session))

        assert count == 1
        assert silent_row.status == state.UNKNOWN
        assert parked_row.status == state.SUBMITTED
        assert held_row.status == state.SUBMITTED
        assert len(harness.calls["events"]) == 1

    def test_nothing_to_sweep_touches_nothing(self, harness):
        count, _ = sweep([])
        assert count == 0
        assert harness.calls["events"] == []


# --- The SQL pre-filter -----------------------------------------------------
class TestTheQuery:
    """Compiled against the real dialect, because the stub cannot fail on it."""

    @pytest.fixture
    def sql(self, harness):
        _count, session = sweep([])
        return str(session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True}))

    def test_it_selects_the_schedule_alongside_the_attempt(self, sql):
        """Read in the same query, not per row: an N+1 inside reconciliation, or
        a request served stale from the identity map, would both be worse."""
        select_list = sql.split("\nFROM")[0]
        assert "JOIN publish_requests" in sql
        assert "publish_requests.scheduled_for" in select_list

    def test_it_locks_only_the_attempt(self, sql):
        """`FOR UPDATE` without `OF` would lock the request row too, contending
        with every other pass that touches the same request — dispatch, poll and
        `refresh_request_status` all do."""
        assert "FOR UPDATE OF publish_attempts SKIP LOCKED" in sql
        assert "FOR UPDATE OF publish_requests" not in sql

    def test_both_holds_are_pre_filtered(self, sql):
        # The pre-filter has to be narrower than the row count, not just correct:
        # every row it returns is a row the Python guard then re-examines.
        assert "publish_attempts.deferred_until IS NULL" in sql
        assert "publish_requests.scheduled_for IS NULL" in sql
