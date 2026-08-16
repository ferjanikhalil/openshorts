"""Autopilot: unattended multi-video clipping + auto-styling.

Autopilot fans N videos through the EXISTING job queue and, when each video's
clips are ready, auto-runs the EXISTING batch pipeline (batch.run_batch) using a
saved styling recipe. It is an additive subsystem — a parent record that tracks
child job_ids — and never touches the single-job dashboard flow.

This module is deliberately framework-free: it owns the in-memory registry and
the state/aggregation logic. app.py wires the HTTP endpoints and the auto-apply
seam that consume it. Mirrors the saas_jobs precedent (self-contained registry
with its own lifecycle, separate from `jobs`).

Key/secret handling: to dub (translate) or run BYOK auto_edit hours after the
user walked away, the ElevenLabs/Gemini keys must live server-side for the
batch's lifetime. They are held in memory only inside the batch record and are
NEVER written to disk — consistent with how a job's env already holds the Gemini
key in memory per job.
"""
import threading
import time
from typing import Dict, List, Optional

# batch_id -> record. A record is:
#   {
#     "batch_id": str,
#     "user_id": Optional[str],           # owner (managed mode); None for BYOK/self-host
#     "recipe": dict,                     # RenderOptions.model_dump() (batch default)
#     "video_overrides": {job_id: dict},  # sparse per-video overrides, by child job_id
#     "sources": [{job_id, label, kind}], # display metadata per child
#     "child_job_ids": [str, ...],
#     "keys": {"gemini": str|None, "elevenlabs": str|None},  # in-memory ONLY
#     "publish": dict|None,               # optional auto-publish plan; see below
#     "status": "running" | "completed" | "cancelled",
#     "cancelled": bool,                  # set by cancel(); auto-apply seam checks it
#     "created_at": float,
#     "finished_at": float|None,          # set once every child reaches a terminal
#                                         # stage (or on cancel) so the board can
#                                         # show real duration, not time-since-start
#   }
autopilot_batches: Dict[str, dict] = {}

_lock = threading.Lock()

# Per-video stages surfaced on the board. A child job walks:
#   queued -> processing -> clips_ready -> editing -> done   (or -> failed)
STAGE_QUEUED = "queued"
STAGE_PROCESSING = "processing"
STAGE_CLIPS_READY = "clips_ready"
STAGE_EDITING = "editing"
STAGE_DONE = "done"
STAGE_FAILED = "failed"


def register_batch(batch_id: str, user_id: Optional[str], recipe: dict,
                   keys: dict, publish: Optional[dict] = None) -> dict:
    """Create and store a batch record. Child jobs are attached as they are enqueued.

    ``publish`` is the optional auto-publish plan, carried here rather than
    derived later because the choice belongs to the moment the batch was
    submitted. Shape (all keys optional except one target):

        {"destination_ids": [...], "group_ids": [...], "platforms": [...],
         "clip_indexes": [0, 1, ...], "max_clips": int, "schedule": "spread"}

    None (the default) means "style the clips and stop" — the behaviour every
    existing autopilot batch has today.
    """
    record = {
        "batch_id": batch_id,
        "user_id": user_id,
        "recipe": recipe,
        "video_overrides": {},
        "sources": [],
        "child_job_ids": [],
        "keys": keys or {},
        "publish": publish or None,
        "status": "running",
        "cancelled": False,
        "created_at": time.time(),
        "finished_at": None,
    }
    with _lock:
        autopilot_batches[batch_id] = record
    return record


def attach_child(batch_id: str, job_id: str, label: str, kind: str,
                 override: Optional[dict] = None) -> None:
    """Record a child job under a batch (with its display label and any override)."""
    with _lock:
        rec = autopilot_batches.get(batch_id)
        if not rec:
            return
        rec["child_job_ids"].append(job_id)
        rec["sources"].append({"job_id": job_id, "label": label, "kind": kind})
        if override:
            rec["video_overrides"][job_id] = override


def set_publish_override(batch_id: str, job_id: str, publish: dict) -> None:
    """Set a per-video publish override. When set, this video publishes to these
    groups instead of the batch-level publish plan. Shape matches the batch plan."""
    with _lock:
        rec = autopilot_batches.get(batch_id)
        if not rec:
            return
        rec.setdefault("publish_overrides", {})[job_id] = publish


def batch_for_job(job_id: str) -> Optional[dict]:
    """Return the batch record owning this child job, or None. Used by the
    auto-apply seam and the retention sweeper to recognize autopilot children."""
    with _lock:
        for rec in autopilot_batches.values():
            if job_id in rec["child_job_ids"]:
                return rec
    return None


def is_autopilot_child(job_id: str) -> bool:
    return batch_for_job(job_id) is not None


def keys_for_job(job_id: str) -> dict:
    rec = batch_for_job(job_id)
    return rec["keys"] if rec else {}


def publish_plan_for_job(job_id: str) -> Optional[dict]:
    """The auto-publish plan for one child job, or None.

    Checks for a per-video override first; if not set, falls back to the batch-level
    plan. Returns None both when the job is not an autopilot child and when neither
    override nor batch plan exists. A cancelled batch also returns None: cancel means
    cancel, and clips that were mid-styling when it happened must not post afterwards.
    """
    rec = batch_for_job(job_id)
    if not rec or rec.get("cancelled"):
        return None
    # Check per-video override first
    plan = rec.get("publish_overrides", {}).get(job_id)
    if not plan:
        # Fall back to batch-level plan
        plan = rec.get("publish")
    if not plan:
        return None
    if not (plan.get("destination_ids") or plan.get("group_ids")):
        return None
    return dict(plan)


def is_cancelled(batch_id: str) -> bool:
    with _lock:
        rec = autopilot_batches.get(batch_id)
        return bool(rec and rec["cancelled"])


def cancel_batch(batch_id: str) -> bool:
    """Mark a batch cancelled. Best-effort: sets a flag the auto-apply seam
    checks before firing a child's batch, and flips status. In-flight per-clip
    batches are cancelled by the caller via their BatchProgress (app.py owns
    `jobs`), since this module intentionally has no reference to the job store."""
    with _lock:
        rec = autopilot_batches.get(batch_id)
        if not rec:
            return False
        rec["cancelled"] = True
        rec["status"] = "cancelled"
        if rec.get("finished_at") is None:
            rec["finished_at"] = time.time()
        return True


def _child_stage(job: Optional[dict]) -> str:
    """Derive a child job's autopilot stage from its job record.

    `job` is the live entry from app.py's `jobs` dict (or None if it was purged).
    A running/completed per-clip batch is read from job['batch'] (a
    batch.BatchProgress); we call .to_dict() so this module needs no import of
    batch and no lock on its internals.
    """
    if job is None:
        return STAGE_QUEUED
    status = job.get("status")
    if status == "failed":
        return STAGE_FAILED
    if status in ("queued", None):
        return STAGE_QUEUED
    if status == "processing":
        return STAGE_PROCESSING

    # status == "completed": clips exist. Look at the auto-batch, if any.
    batch = job.get("batch")
    bstate = None
    if batch is not None and hasattr(batch, "to_dict"):
        try:
            bstate = batch.to_dict()
        except Exception:
            bstate = None

    if bstate is None:
        # No per-clip batch attached. Either styling hasn't started yet, or the
        # auto-apply seam decided none will run (nothing enabled / cancelled /
        # unreadable recipe) and marked the job 'skipped'. Skipped must be
        # terminal, otherwise a no-styling batch never completes and the board
        # spins forever.
        if job.get("autopilot_styling") == "skipped":
            return STAGE_DONE
        return STAGE_CLIPS_READY
    if bstate.get("status") == "running":
        return STAGE_EDITING
    # batch finished (completed/cancelled) -> the video is done styling.
    return STAGE_DONE


def progress(batch_id: str, jobs: dict) -> Optional[dict]:
    """Aggregate child-job states into a per-video stage board.

    `jobs` is app.py's live job store, passed in so this module stays
    framework/state-free. Returns None if the batch id is unknown.
    """
    with _lock:
        rec = autopilot_batches.get(batch_id)
        if not rec:
            return None
        sources = list(rec["sources"])
        status = rec["status"]
        cancelled = rec["cancelled"]
        created_at = rec["created_at"]
        finished_at = rec.get("finished_at")

    videos = []
    for src in sources:
        job = jobs.get(src["job_id"])
        stage = _child_stage(job)
        entry = {
            "job_id": src["job_id"],
            "label": src["label"],
            "kind": src["kind"],
            "stage": stage,
        }
        result = (job or {}).get("result") or {}
        clips = result.get("clips") or []
        entry["clip_count"] = len(clips)
        batch = (job or {}).get("batch")
        if batch is not None and hasattr(batch, "to_dict"):
            try:
                entry["batch"] = batch.to_dict()
            except Exception:
                pass

        # Surface publishing status from job logs (appended by _schedule_autopublish).
        logs = (job or {}).get("logs") or []
        publish_log = next((l for l in logs if l.startswith("Publishing:")), None)
        if publish_log:
            entry["publishing_status"] = "published"
            entry["publishing_message"] = publish_log

        videos.append(entry)

    stages = [v["stage"] for v in videos]
    terminal = {STAGE_DONE, STAGE_FAILED}
    # A batch is "running" until every child reaches a terminal stage. Once all
    # are terminal, mark completed (unless it was explicitly cancelled).
    if not cancelled and videos and all(s in terminal for s in stages):
        with _lock:
            r = autopilot_batches.get(batch_id)
            if r and r["status"] == "running":
                r["status"] = "completed"
                r["finished_at"] = time.time()
            if r:
                finished_at = r.get("finished_at")
        status = "completed"

    return {
        "batch_id": batch_id,
        "status": status,
        "cancelled": cancelled,
        "created_at": created_at,
        "finished_at": finished_at,
        "total": len(videos),
        "done": sum(1 for s in stages if s == STAGE_DONE),
        "failed": sum(1 for s in stages if s == STAGE_FAILED),
        "videos": videos,
    }


def list_batches(user_id: Optional[str], jobs: dict) -> list:
    """Return progress summaries for every batch the caller owns.

    Ownership matches _assert_job_owner semantics: in self-host (no user_id on
    records) everything is visible; in managed mode only the caller's batches.
    """
    with _lock:
        ids = [
            bid for bid, rec in autopilot_batches.items()
            if rec.get("user_id") is None or str(rec.get("user_id")) == str(user_id)
        ]
    out = []
    for bid in ids:
        p = progress(bid, jobs)
        if p:
            out.append(p)
    out.sort(key=lambda p: p["created_at"], reverse=True)
    return out


def cleanup_expired(jobs: dict, ttl_seconds: int) -> None:
    """Drop batch records that are terminal and older than ttl_seconds. Mirrors
    the saas_jobs memory sweep. Never removes a still-running batch."""
    now = time.time()
    with _lock:
        expired = [
            bid for bid, rec in autopilot_batches.items()
            if rec["status"] in ("completed", "cancelled")
            and now - rec["created_at"] > ttl_seconds
        ]
        for bid in expired:
            del autopilot_batches[bid]
