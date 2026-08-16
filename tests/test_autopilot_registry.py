"""Unit tests for autopilot.py — the batch registry and the per-video stage machine.

Companion to test_autopilot.py, which covers the render_options.py additions
(resolve_cascade / render_options_to_operations). This file covers the
orchestrator: registration, child attachment, stage derivation from live job
records, batch-level aggregation, ownership filtering, cancel and expiry.

autopilot.autopilot_batches is module-level mutable state, so every test starts
from a cleared registry (autoclean fixture) to stay order-independent.
"""
import time

import pytest

import autopilot as ap


@pytest.fixture(autouse=True)
def _clear_registry():
    ap.autopilot_batches.clear()
    yield
    ap.autopilot_batches.clear()


class _FakeBatch:
    """Stands in for batch.BatchProgress — _child_stage only calls .to_dict()."""

    def __init__(self, status="running"):
        self._status = status

    def to_dict(self):
        return {"status": self._status}


class _ExplodingBatch:
    """A BatchProgress whose to_dict() raises; must not take the board down."""

    def to_dict(self):
        raise RuntimeError("boom")


def _batch(batch_id="b1", user_id=None, keys=None):
    return ap.register_batch(batch_id, user_id, {"subtitles": {"enabled": True}},
                             keys or {})


def _job(status="completed", clips=0, batch=None, styling=None):
    job = {"status": status, "result": {"clips": [{"i": n} for n in range(clips)]}}
    if batch is not None:
        job["batch"] = batch
    if styling is not None:
        job["autopilot_styling"] = styling
    return job


class TestRegistry:
    def test_register_and_attach(self):
        _batch()
        ap.attach_child("b1", "j1", "video one", "url")
        rec = ap.autopilot_batches["b1"]
        assert rec["child_job_ids"] == ["j1"]
        assert rec["sources"] == [{"job_id": "j1", "label": "video one", "kind": "url"}]
        assert rec["status"] == "running"
        assert rec["finished_at"] is None

    def test_attach_stores_sparse_override_only_when_given(self):
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        ap.attach_child("b1", "j2", "b", "file", override={"hook": {"enabled": True}})
        rec = ap.autopilot_batches["b1"]
        assert "j1" not in rec["video_overrides"]
        assert rec["video_overrides"]["j2"] == {"hook": {"enabled": True}}

    def test_attach_to_unknown_batch_is_noop(self):
        ap.attach_child("nope", "j1", "a", "url")  # must not raise
        assert ap.autopilot_batches == {}

    def test_batch_for_job_and_is_child(self):
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        assert ap.batch_for_job("j1")["batch_id"] == "b1"
        assert ap.is_autopilot_child("j1") is True
        assert ap.batch_for_job("stranger") is None
        assert ap.is_autopilot_child("stranger") is False

    def test_keys_are_in_memory_only_and_scoped_to_the_batch(self):
        # Keys must never be written to disk; they live only on the record and
        # are reachable exclusively through a job that belongs to that batch.
        _batch(keys={"gemini": "G", "elevenlabs": "E"})
        ap.attach_child("b1", "j1", "a", "url")
        assert ap.keys_for_job("j1") == {"gemini": "G", "elevenlabs": "E"}
        assert ap.keys_for_job("not-a-child") == {}


class TestChildStage:
    def test_missing_job_is_queued(self):
        assert ap._child_stage(None) == ap.STAGE_QUEUED

    @pytest.mark.parametrize("status,expected", [
        ("queued", ap.STAGE_QUEUED),
        (None, ap.STAGE_QUEUED),
        ("processing", ap.STAGE_PROCESSING),
        ("failed", ap.STAGE_FAILED),
    ])
    def test_pre_completion_statuses(self, status, expected):
        assert ap._child_stage({"status": status}) == expected

    def test_completed_without_batch_waits_at_clips_ready(self):
        # Styling is expected but the worker thread hasn't attached its
        # BatchProgress yet — a transient, non-terminal state.
        assert ap._child_stage(_job()) == ap.STAGE_CLIPS_READY

    def test_completed_with_running_batch_is_editing(self):
        assert ap._child_stage(_job(batch=_FakeBatch("running"))) == ap.STAGE_EDITING

    @pytest.mark.parametrize("bstatus", ["completed", "cancelled"])
    def test_completed_with_finished_batch_is_done(self, bstatus):
        assert ap._child_stage(_job(batch=_FakeBatch(bstatus))) == ap.STAGE_DONE

    def test_skipped_styling_is_terminal(self):
        # Regression: the auto-apply seam returns early (nothing enabled, batch
        # cancelled, or unreadable recipe) and no BatchProgress is ever attached.
        # Without the 'skipped' marker this stayed at clips_ready forever, so the
        # batch never completed and the board spun indefinitely.
        assert ap._child_stage(_job(styling="skipped")) == ap.STAGE_DONE

    def test_running_styling_marker_still_waits(self):
        # 'running' means a batch IS coming; don't call it done before it lands.
        assert ap._child_stage(_job(styling="running")) == ap.STAGE_CLIPS_READY

    def test_broken_batch_object_degrades_to_clips_ready(self):
        assert ap._child_stage(_job(batch=_ExplodingBatch())) == ap.STAGE_CLIPS_READY


class TestProgress:
    def test_unknown_batch_returns_none(self):
        assert ap.progress("ghost", {}) is None

    def test_counts_and_clip_totals(self):
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        ap.attach_child("b1", "j2", "b", "url")
        ap.attach_child("b1", "j3", "c", "file")
        jobs = {
            "j1": _job(clips=3, batch=_FakeBatch("completed")),
            "j2": _job(status="failed"),
            "j3": _job(status="processing"),
        }
        p = ap.progress("b1", jobs)
        assert p["total"] == 3
        assert p["done"] == 1
        assert p["failed"] == 1
        assert p["status"] == "running"          # j3 still going
        assert p["finished_at"] is None
        assert [v["stage"] for v in p["videos"]] == ["done", "failed", "processing"]
        assert p["videos"][0]["clip_count"] == 3
        assert p["videos"][0]["batch"] == {"status": "completed"}

    def test_completes_and_stamps_finished_at_when_all_terminal(self):
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        ap.attach_child("b1", "j2", "b", "url")
        jobs = {"j1": _job(batch=_FakeBatch("completed")), "j2": _job(status="failed")}
        before = time.time()
        p = ap.progress("b1", jobs)
        assert p["status"] == "completed"
        assert p["finished_at"] is not None
        assert p["finished_at"] >= before
        assert ap.autopilot_batches["b1"]["status"] == "completed"

    def test_finished_at_is_frozen_not_recomputed(self):
        # The board reports "Took X" off this stamp; re-polling a finished batch
        # tomorrow must not restate the duration as time-since-start.
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        jobs = {"j1": _job(batch=_FakeBatch("completed"))}
        first = ap.progress("b1", jobs)["finished_at"]
        time.sleep(0.01)
        assert ap.progress("b1", jobs)["finished_at"] == first

    def test_all_skipped_batch_reaches_completed(self):
        # End-to-end shape of the regression: a recipe with no styling modules
        # must still land the whole batch on 'completed'.
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        ap.attach_child("b1", "j2", "b", "url")
        jobs = {"j1": _job(clips=2, styling="skipped"),
                "j2": _job(clips=1, styling="skipped")}
        p = ap.progress("b1", jobs)
        assert p["status"] == "completed"
        assert p["done"] == 2

    def test_empty_batch_stays_running(self):
        # No children attached yet (submit in flight) — all() over an empty list
        # is True, so guard against declaring victory before any video exists.
        _batch()
        p = ap.progress("b1", {})
        assert p["total"] == 0
        assert p["status"] == "running"

    def test_cancelled_batch_never_flips_to_completed(self):
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        ap.cancel_batch("b1")
        p = ap.progress("b1", {"j1": _job(batch=_FakeBatch("cancelled"))})
        assert p["status"] == "cancelled"
        assert p["cancelled"] is True

    def test_purged_child_job_is_reported_not_crashed(self):
        _batch()
        ap.attach_child("b1", "j1", "a", "url")
        p = ap.progress("b1", {})   # job gone from the store
        assert p["videos"][0]["stage"] == ap.STAGE_QUEUED
        assert p["videos"][0]["clip_count"] == 0


class TestCancel:
    def test_cancel_sets_flag_status_and_finished_at(self):
        _batch()
        assert ap.cancel_batch("b1") is True
        rec = ap.autopilot_batches["b1"]
        assert rec["cancelled"] is True
        assert rec["status"] == "cancelled"
        assert rec["finished_at"] is not None
        assert ap.is_cancelled("b1") is True

    def test_cancel_unknown_batch_returns_false(self):
        assert ap.cancel_batch("ghost") is False
        assert ap.is_cancelled("ghost") is False

    def test_cancel_twice_keeps_the_first_stamp(self):
        _batch()
        ap.cancel_batch("b1")
        first = ap.autopilot_batches["b1"]["finished_at"]
        time.sleep(0.01)
        ap.cancel_batch("b1")
        assert ap.autopilot_batches["b1"]["finished_at"] == first


class TestListBatches:
    def test_self_host_records_visible_to_everyone(self):
        _batch("b1", user_id=None)
        assert len(ap.list_batches(None, {})) == 1
        assert len(ap.list_batches("u9", {})) == 1

    def test_managed_mode_filters_by_owner(self):
        _batch("b1", user_id="u1")
        _batch("b2", user_id="u2")
        mine = ap.list_batches("u1", {})
        assert [b["batch_id"] for b in mine] == ["b1"]

    def test_owner_match_is_string_insensitive_to_type(self):
        # user ids arrive as ints from the DB and strings from the token.
        _batch("b1", user_id=7)
        assert [b["batch_id"] for b in ap.list_batches("7", {})] == ["b1"]

    def test_sorted_newest_first(self):
        _batch("old")
        time.sleep(0.01)
        _batch("new")
        assert [b["batch_id"] for b in ap.list_batches(None, {})] == ["new", "old"]


class TestCleanupExpired:
    def test_drops_terminal_records_past_ttl(self):
        _batch("b1")
        ap.cancel_batch("b1")
        ap.autopilot_batches["b1"]["created_at"] = time.time() - 10_000
        ap.cleanup_expired({}, ttl_seconds=100)
        assert "b1" not in ap.autopilot_batches

    def test_keeps_running_batches_regardless_of_age(self):
        # An unattended batch can legitimately run for hours; age alone must
        # never purge one that is still working.
        _batch("b1")
        ap.autopilot_batches["b1"]["created_at"] = time.time() - 10_000
        ap.cleanup_expired({}, ttl_seconds=100)
        assert "b1" in ap.autopilot_batches

    def test_keeps_recent_terminal_batches(self):
        _batch("b1")
        ap.cancel_batch("b1")
        ap.cleanup_expired({}, ttl_seconds=10_000)
        assert "b1" in ap.autopilot_batches
