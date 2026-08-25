"""Who is allowed to cancel a slotted assignment, and who must leave it alone.

``run_due_assignments`` is the clock side of publishing: it turns an assignment
that has a slot into a real request with attempts. To do that it needs the clip,
which it gets from ``clips.resolve`` — and ``resolve`` answers ``None`` for two
completely different reasons that used to share one branch:

  * the clip is genuinely gone (``JOB_RETENTION_SECONDS`` deleted it), which is
    terminal and correctly cancels the assignment;
  * **this process holds no clip files at all.** The always-on publisher
    (``runner.py``) registers no resolver on purpose — the machine that renders
    clips is a different one and may be asleep — so ``resolve`` there returns
    ``None`` for every clip that will ever exist.

Collapsing those meant the publisher cancelled every rhythm assignment it
reached, terminally, usually within seconds of the slot being assigned and
always before the rendering host got a pass. Nothing revisits a ``cancelled``
row, so the plan was destroyed with one event and no attempt ever created. This
suite pins the distinction, because both halves fail silently: too eager and
plans vanish, too shy and a genuinely missing clip is retried forever.

Stub session, no Postgres, no network — runs in CI with the other pure suites.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from publishing import planner  # noqa: E402
from publishing.models import PublishDestination  # noqa: E402

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
SLOT = NOW + timedelta(hours=9)          # comfortably future: no catch-up

GROUP = "7c9f1e2a-0000-4000-8000-0000000000aa"
JOB = "45dd7cf4-23e2-40f4-baf0-f2148a879e8a"
REQUEST = "7c9f1e2a-0000-4000-8000-0000000000bb"

PAYLOAD = {"title": "t", "caption": "c"}
CLIP_INFO = {"filename": "clip_1.mp4", "output_dir": "/out",
             "fingerprint": "fp_abc", "title": "t", "caption": "c"}


def run(coro):
    return asyncio.run(coro)


def make_assignment(**over):
    row = SimpleNamespace(
        id="7c9f1e2a-0000-4000-8000-0000000000cc",
        publish_group_id=GROUP,
        job_id=JOB,
        clip_index=0,
        status="pending",
        scheduled_for=SLOT,
        user_id=None,
        publish_request_id=None,
        meta={"await_slot": True, "payload": PAYLOAD, "source": "planner"},
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def make_destination(platform="tiktok", health="ok"):
    return SimpleNamespace(
        id=f"dest-{platform}", publish_group_id=GROUP, provider="zernio",
        platform=platform, enabled=True, health=health, credential_slot=None)


class StubSession:
    """Answers the two queries the function makes.

    First ``execute`` is the assignment claim, every later one is a destination
    lookup — dispatched on the statement's mapped entity rather than call order,
    so a reordering upstream surfaces as a failing assertion, not silent
    nonsense.
    """

    def __init__(self, assignments, destinations):
        self._assignments = assignments
        self._destinations = destinations
        self.executed = 0

    async def execute(self, stmt):
        self.executed += 1
        entity = (stmt.column_descriptions or [{}])[0].get("entity")
        rows = list(self._destinations) if entity is PublishDestination \
            else list(self._assignments)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, _model, _ident):
        return None


@pytest.fixture
def harness(monkeypatch):
    calls = {"events": [], "requests": []}

    async def log_event(_session, kind, message="", **kw):
        calls["events"].append((kind, message))

    async def create_request(_session, **kw):
        calls["requests"].append(kw)
        return SimpleNamespace(id=REQUEST)

    monkeypatch.setattr(planner.service, "log_event", log_event)
    monkeypatch.setattr(planner.service, "create_request", create_request)
    monkeypatch.setattr(planner, "_now", lambda: NOW)
    # Warn-once state is module-level; a leaked True would mask a real call.
    monkeypatch.setattr(planner, "_warned_no_resolver", False)
    return SimpleNamespace(calls=calls)


def convert(harness, *, resolves, has_resolver, assignment=None,
            destinations=None, monkeypatch=None):
    row = assignment or make_assignment()
    session = StubSession([row], destinations or [make_destination()])
    mp = monkeypatch or pytest.MonkeyPatch()
    mp.setattr(planner.clips_mod, "resolve",
               lambda *_a, **_k: CLIP_INFO if resolves else None)
    mp.setattr(planner.clips_mod, "has_resolver", lambda: has_resolver)
    try:
        done = run(planner.run_due_assignments(session))
    finally:
        if monkeypatch is None:
            mp.undo()
    return done, row


def kinds(harness):
    return [k for k, _m in harness.calls["events"]]


class TestProcessWithoutClipFiles:
    """The publisher-role case: `None` means "not mine", never "gone"."""

    def test_the_assignment_is_left_pending(self, harness):
        done, row = convert(harness, resolves=False, has_resolver=False)
        assert row.status == "pending"
        assert done == 0

    def test_nothing_is_cancelled_and_no_clip_missing_event_is_logged(
            self, harness):
        """The bug's fingerprint. `cancelled` is terminal and unrevisited, so
        this single assertion is the difference between a plan that publishes
        nine hours later and one that is gone."""
        _done, row = convert(harness, resolves=False, has_resolver=False)
        assert row.status != "cancelled"
        assert "assignment.clip_missing" not in kinds(harness)

    def test_no_request_is_created(self, harness):
        convert(harness, resolves=False, has_resolver=False)
        assert harness.calls["requests"] == []

    def test_the_slot_is_preserved_for_the_owning_host(self, harness):
        """Parking is only useful if the slot survives it — the rendering host
        must convert to the *same* time, not re-slot."""
        _done, row = convert(harness, resolves=False, has_resolver=False)
        assert row.scheduled_for == SLOT

    def test_it_says_so_once_per_process(self, harness, capsys):
        convert(harness, resolves=False, has_resolver=False)
        convert(harness, resolves=False, has_resolver=False)
        printed = capsys.readouterr().out
        assert printed.count("no clip resolver on this process") == 1


class TestProcessThatOwnsTheClips:
    """A registered resolver saying `None` is authoritative: the clip is gone."""

    def test_a_genuinely_missing_clip_is_cancelled(self, harness):
        done, row = convert(harness, resolves=False, has_resolver=True)
        assert row.status == "cancelled"
        assert "assignment.clip_missing" in kinds(harness)
        assert done == 1

    def test_cancelling_creates_no_request(self, harness):
        convert(harness, resolves=False, has_resolver=True)
        assert harness.calls["requests"] == []

    def test_a_resolvable_clip_still_converts(self, harness):
        """The guard must not cost the happy path."""
        done, row = convert(harness, resolves=True, has_resolver=True)
        assert row.status == "requested"
        assert row.publish_request_id == REQUEST
        assert done == 1
        assert len(harness.calls["requests"]) == 1

    def test_the_frozen_payload_and_slot_are_what_get_requested(self, harness):
        """Rhythm meta froze the caption at plan time; conversion must not
        re-derive it, and the request must carry the assignment's own slot."""
        convert(harness, resolves=True, has_resolver=True)
        kw = harness.calls["requests"][0]
        assert kw["payload"] == PAYLOAD
        assert kw["scheduled_for"] == SLOT
        assert kw["mode"] == "scheduled"
        assert kw["job_id"] == JOB
        assert kw["content_fingerprint"] == "fp_abc"


class TestOrderingAgainstTheOtherGuards:
    def test_a_slotless_rhythm_row_is_untouched_either_way(self, harness):
        """It has no slot yet, so neither branch should be reached at all."""
        for has_resolver in (True, False):
            row = make_assignment(scheduled_for=None)
            _done, row = convert(harness, resolves=False,
                                 has_resolver=has_resolver, assignment=row)
            assert row.status == "pending"
            assert "assignment.clip_missing" not in kinds(harness)

    def test_no_usable_destination_parks_before_the_clip_is_consulted(
            self, harness):
        """Pre-existing behaviour, pinned here because the two park paths are
        easy to conflate: a disconnected account is also not a cancellation."""
        _done, row = convert(
            harness, resolves=True, has_resolver=True,
            destinations=[make_destination(health="disconnected")])
        assert row.status == "pending"
        assert "assignment.no_destinations" in kinds(harness)
