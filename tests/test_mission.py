"""Unit tests for llm/mission.py — checkpoint read/write/execute."""
import json
import os
import pytest

from llm.mission import (
    execute_mission,
    load_completed_mission,
    load_transcript,
    missions_dir,
    save_mission_result,
    save_transcript,
    write_manifest,
)


@pytest.fixture
def job_dir(tmp_path):
    return str(tmp_path / "job_001")


class TestAtomicWriteAndLoad:
    def test_save_and_load_roundtrip(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        result = {"windows": [{"id": "w1", "score": 85}]}
        cost = {"input_tokens": 100, "output_tokens": 50, "total_cost": 0.001}
        save_mission_result(job_dir, "score_batch_000", result, cost, "gemini")

        loaded = load_completed_mission(job_dir, "score_batch_000")
        assert loaded is not None
        assert loaded["status"] == "completed"
        assert loaded["result"] == result
        assert loaded["cost"] == cost
        assert loaded["provider"] == "gemini"

    def test_load_returns_none_for_missing(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        assert load_completed_mission(job_dir, "nonexistent") is None

    def test_load_returns_none_for_corrupt(self, job_dir):
        os.makedirs(missions_dir(job_dir), exist_ok=True)
        path = os.path.join(missions_dir(job_dir), "bad.json")
        with open(path, "w") as f:
            f.write("not json {{{")
        assert load_completed_mission(job_dir, "bad") is None

    def test_load_returns_none_for_incomplete_status(self, job_dir):
        os.makedirs(missions_dir(job_dir), exist_ok=True)
        path = os.path.join(missions_dir(job_dir), "partial.json")
        with open(path, "w") as f:
            json.dump({"status": "running", "result": {}}, f)
        assert load_completed_mission(job_dir, "partial") is None

    def test_no_tmp_file_left_after_save(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        save_mission_result(job_dir, "m1", {"x": 1})
        tmp_path = os.path.join(missions_dir(job_dir), "m1.json.tmp")
        assert not os.path.exists(tmp_path)


class TestManifest:
    def test_write_and_read_manifest(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        ids = ["score_batch_000", "score_batch_001", "detail"]
        write_manifest(job_dir, ids, "scoring")

        manifest_path = os.path.join(missions_dir(job_dir), "manifest.json")
        assert os.path.isfile(manifest_path)
        with open(manifest_path) as f:
            data = json.load(f)
        assert data["phase"] == "scoring"
        assert data["missions"] == ids


class TestExecuteMission:
    def test_skips_fn_when_checkpoint_exists(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        save_mission_result(job_dir, "m1", {"cached": True}, {"total_cost": 0})

        call_count = [0]

        def fn():
            call_count[0] += 1
            return {"cached": False}, None

        result, cost = execute_mission(job_dir, "m1", fn)
        assert call_count[0] == 0
        assert result == {"cached": True}

    def test_calls_fn_and_persists_when_no_checkpoint(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)

        def fn():
            return {"fresh": True}, {"total_cost": 0.5}

        result, cost = execute_mission(job_dir, "m2", fn, provider_name="test")
        assert result == {"fresh": True}
        assert cost == {"total_cost": 0.5}

        loaded = load_completed_mission(job_dir, "m2")
        assert loaded is not None
        assert loaded["result"] == {"fresh": True}
        assert loaded["provider"] == "test"

    def test_propagates_fn_exception_without_checkpoint(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)

        def fn():
            raise RuntimeError("provider down")

        with pytest.raises(RuntimeError, match="provider down"):
            execute_mission(job_dir, "m3", fn)

        assert load_completed_mission(job_dir, "m3") is None


class TestTranscriptCheckpoint:
    def test_save_and_load_roundtrip(self, job_dir):
        transcript = {
            "text": "hello world",
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello world",
                          "words": [{"word": " hello", "start": 0.0, "end": 0.5}]}],
        }
        save_transcript(job_dir, transcript)
        assert load_transcript(job_dir) == transcript

    def test_load_returns_none_for_missing(self, job_dir):
        assert load_transcript(job_dir) is None

    def test_load_returns_none_for_corrupt(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, ".transcript.json"), "w") as f:
            f.write("not json {{{")
        assert load_transcript(job_dir) is None

    def test_none_output_dir_is_safe(self):
        # Callers pass output_dir=None for ad-hoc runs; must not raise.
        assert load_transcript(None) is None
        save_transcript(None, {"segments": []})  # no-op, no exception


class TestClipCheckpoint:
    """Clips reuse the generic mission checkpoint under a clip_NNN id."""

    def test_clip_checkpoint_skips_rerender(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        save_mission_result(job_dir, "clip_000", {"filename": "vid_clip_1.mp4"})
        loaded = load_completed_mission(job_dir, "clip_000")
        assert loaded is not None
        assert loaded["result"]["filename"] == "vid_clip_1.mp4"

    def test_missing_clip_checkpoint_is_none(self, job_dir):
        os.makedirs(job_dir, exist_ok=True)
        assert load_completed_mission(job_dir, "clip_005") is None
