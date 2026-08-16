"""Mission-based checkpointing for the LLM pipeline.

Each LLM call (scoring batch or detail pass) is a "mission". Completed missions
are persisted as JSON files in <output_dir>/missions/. On restart, completed
missions are loaded from disk and skipped.

Atomic writes: write to a .tmp file, then os.replace() to the final name.
This guarantees a mission file is either fully written or absent — never corrupt.
"""
import json
import os
import time
from typing import Callable, List, Optional, Tuple

MISSIONS_DIR_NAME = "missions"
MANIFEST_FILE = "manifest.json"
TRANSCRIPT_FILE = ".transcript.json"


def missions_dir(output_dir: str) -> str:
    return os.path.join(output_dir, MISSIONS_DIR_NAME)


def _mission_path(output_dir: str, mission_id: str) -> str:
    return os.path.join(missions_dir(output_dir), f"{mission_id}.json")


def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_completed_mission(output_dir: str, mission_id: str) -> Optional[dict]:
    """Return the checkpoint payload if the mission completed, else None."""
    path = _mission_path(output_dir, mission_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("status") == "completed":
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def save_mission_result(
    output_dir: str,
    mission_id: str,
    result: dict,
    cost: Optional[dict] = None,
    provider_name: str = "",
) -> None:
    """Persist a completed mission's result atomically."""
    os.makedirs(missions_dir(output_dir), exist_ok=True)
    payload = {
        "mission_id": mission_id,
        "status": "completed",
        "result": result,
        "cost": cost,
        "provider": provider_name,
        "completed_at": time.time(),
    }
    _atomic_write_json(_mission_path(output_dir, mission_id), payload)


def write_manifest(output_dir: str, mission_ids: List[str], phase: str) -> None:
    """Write the mission manifest listing all expected missions for this run."""
    os.makedirs(missions_dir(output_dir), exist_ok=True)
    manifest = {
        "phase": phase,
        "missions": mission_ids,
        "written_at": time.time(),
    }
    _atomic_write_json(os.path.join(missions_dir(output_dir), MANIFEST_FILE), manifest)


def execute_mission(
    output_dir: str,
    mission_id: str,
    fn: Callable[[], Tuple[dict, Optional[dict]]],
    provider_name: str = "",
) -> Tuple[dict, Optional[dict]]:
    """Execute a mission with checkpoint-or-replay semantics.

    If a completed checkpoint exists, load and return it (skip the LLM call).
    Otherwise, call fn() which returns (result_dict, cost_dict), then persist.
    """
    existing = load_completed_mission(output_dir, mission_id)
    if existing is not None:
        print(f"   \u23ed\ufe0f  Mission {mission_id}: loaded from checkpoint")
        return existing["result"], existing.get("cost")

    result, cost = fn()
    save_mission_result(output_dir, mission_id, result, cost, provider_name)
    return result, cost


# --- Transcript checkpoint ---------------------------------------------------
# Transcription is the single most expensive non-clip stage on a long video and,
# unlike scoring/detail, is one atomic call (no sub-missions). Cache its result
# so a resumed run skips it entirely. Kept at the job root (not under missions/)
# because it is the pipeline's shared input, not one LLM call.

def load_transcript(output_dir: str) -> Optional[dict]:
    """Return the cached transcript dict if present and parseable, else None."""
    if not output_dir:
        return None
    path = os.path.join(output_dir, TRANSCRIPT_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_transcript(output_dir: str, transcript: dict) -> None:
    """Persist the transcript atomically so a resumed run can skip re-transcribing."""
    if not output_dir:
        return
    os.makedirs(output_dir, exist_ok=True)
    _atomic_write_json(os.path.join(output_dir, TRANSCRIPT_FILE), transcript)
