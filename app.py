import os
import re
import sys
import uuid
import subprocess
import threading
import json
import shutil
import glob
import time
import zipfile
import math
import itertools
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from typing import Dict, Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.requests import Request as StarletteRequest
from pydantic import BaseModel

# Raise the multipart per-part size limit (default 1 MB) so that text form
# fields carrying base64 logos inside the branding JSON are not rejected.
_MAX_PART_SIZE = 10 * 1024 * 1024  # 10 MB
_original_form = StarletteRequest.form


def _form_with_larger_parts(self, *args, **kwargs):
    kwargs.setdefault("max_part_size", _MAX_PART_SIZE)
    return _original_form(self, *args, **kwargs)


StarletteRequest.form = _form_with_larger_parts
from s3_uploader import upload_job_artifacts, list_all_clips, upload_actor_to_s3, list_actor_gallery, upload_video_to_gallery, list_video_gallery
from render_options import (RenderOptions, BrandingConfig, LogoOverlay, load_options, save_options,
                            resolve_options, resolve_branding, rebrand_clip, deep_merge, apply_branding,
                            load_branding, save_branding, resolve_cascade, render_options_to_operations)
import autopilot as autopilot_mod

load_dotenv()

# Constants
UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configuration
# Default to 1 if not set, but user can set higher for powerful servers
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "5"))
MAX_FILE_SIZE_MB = 2048  # 2GB limit
JOB_RETENTION_SECONDS = int(os.environ.get("JOB_RETENTION_SECONDS", "86400"))  # job/file retention: 24 hours (issue #46)
# Ceiling for the working directory once it lives on a persistent volume: the
# age-based sweep alone can't stop a burst of long videos from filling the disk.
# 0 disables the cap.
OUTPUT_MAX_GB = int(os.environ.get("OUTPUT_MAX_GB", "25"))
# Same idea for source uploads, which are the biggest single files on disk.
UPLOADS_MAX_GB = int(os.environ.get("UPLOADS_MAX_GB", "15"))
# Pre-flight quality gate: warn before processing a YouTube source below this
# height (0 disables). Only applies to URLs; uploads are whatever the user gave.
QUALITY_GATE_MIN_HEIGHT = int(os.environ.get("QUALITY_GATE_MIN_HEIGHT", "720"))
QUALITY_PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_probe.py")
DISABLE_YOUTUBE_URL = os.environ.get("DISABLE_YOUTUBE_URL", "false").lower() in ("1", "true", "yes")

# ---- Cloud billing (paid / managed-keys) integration --------------------------
# All paid-mode code lives in the optional `cloud/` package and is imported ONLY
# when BILLING_ENABLED is set. With the flag off, the app behaves exactly as the
# self-hosted BYOK app does today (no extra dependencies required).
BILLING_ENABLED = os.environ.get("BILLING_ENABLED", "").lower() in ("1", "true", "yes")
# Force full pipeline logs to the client even under billing (local debugging).
DEBUG_LOGS = os.environ.get("DEBUG_LOGS", "").lower() in ("1", "true", "yes")

if BILLING_ENABLED:
    import cloud
    from cloud import managed_keys, metering as _metering, config as _cloud_config, alerts as _alerts
    from cloud.auth import get_current_user_optional
else:
    cloud = None
    managed_keys = None
    _metering = None
    _cloud_config = None
    _alerts = None

    async def get_current_user_optional(request: Request):
        # No-op dependency in self-host mode: every request is anonymous / BYOK.
        return None


# ---- Automated social publishing ----------------------------------------------
# Same pattern as `cloud/`: everything lives in the optional `publishing/` package
# and is imported ONLY when the flag is on, so an instance that doesn't publish
# never needs Postgres, `cryptography`, or a master key. Independent of
# BILLING_ENABLED — publishing works in either mode.
PUBLISHING_ENABLED = os.environ.get("PUBLISHING_ENABLED", "").lower() in ("1", "true", "yes")

if PUBLISHING_ENABLED:
    import publishing
    # Imported by name rather than reached for as `publishing.clips` later: the
    # package binds its submodules as a side effect of setup_sync, and depending
    # on that ordering is how a rename turns into an AttributeError at startup.
    from publishing import clips as publish_clips, media as publish_media
    from publishing.crypto import SECRET_PATTERNS as _PUBLISH_SECRET_PATTERNS
else:
    publishing = None
    publish_clips = None
    publish_media = None
    _PUBLISH_SECRET_PATTERNS = []


async def _user_from_request(request: Request):
    """Load the authenticated cloud user (or None). Cheap indexed lookup."""
    return await get_current_user_optional(request)


async def resolve_gemini(request: Request) -> Optional[str]:
    """Resolve the Gemini API key for a request.

    Cloud (hosted) is PAID-ONLY: there is no BYOK for the core pipeline, so the
    ``X-Gemini-Key`` header is ignored — an entitled user (active plan or trial)
    gets the managed server key, everyone else gets ``None`` (→ 402, start trial).
    Self-host keeps BYOK: header wins, else the env fallback.
    """
    if BILLING_ENABLED:
        user = await _user_from_request(request)
        if managed_keys.has_active_entitlement(user):
            return managed_keys.gemini_key()
        return None
    header = request.headers.get("X-Gemini-Key")
    if header:
        return header
    return os.environ.get("GEMINI_API_KEY")


async def resolve_upload_post(request: Request, body_key: Optional[str] = None):
    """Resolve the Upload-Post key and the profile to post as.

    Returns ``(api_key, forced_profile_username_or_None)``. Cloud is paid-only:
    an entitled user gets the managed key + their own forced profile (body key /
    user_id ignored); a non-entitled user gets ``(None, None)``. Self-host keeps
    BYOK: header, then body key, then env.
    """
    if BILLING_ENABLED:
        user = await _user_from_request(request)
        if managed_keys.has_active_entitlement(user):
            profile = await cloud.social_profiles.ensure_profile(user)
            return managed_keys.upload_post_key(), profile
        return None, None
    header = request.headers.get("X-Upload-Post-Key")
    key = header or body_key or os.environ.get("UPLOAD_POST_API_KEY")
    return key, None


async def resolve_llm_env(request: Request) -> Optional[dict]:
    """Resolve LLM provider env vars for the processing subprocess.

    Returns a dict of env vars to inject, or None if no valid config found.
    Backward compatible: X-Gemini-Key alone still works (maps to gemini provider).
    """
    provider = request.headers.get("X-LLM-Provider", "")
    base_url = request.headers.get("X-LLM-Base-URL", "")
    llm_key = request.headers.get("X-LLM-Key", "")
    model = request.headers.get("X-LLM-Model", "")

    if provider == "openai_compat":
        if not base_url or not model:
            return None
        return {
            "LLM_PROVIDER": "openai_compat",
            "OPENAI_BASE_URL": base_url,
            "OPENAI_API_KEY": llm_key or "no-key",
            "OPENAI_MODEL": model,
        }

    # Default: gemini (backward compat with X-Gemini-Key)
    api_key = await resolve_gemini(request)
    if not api_key:
        return None
    env = {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": api_key}
    if model:
        env["GEMINI_MODEL"] = model
    return env


def gemini_missing_error():
    """The right 4xx when no Gemini key could be resolved.

    402 for a signed-in-but-not-entitled cloud user (needs a plan); 400 otherwise
    (BYOK header simply missing).
    """
    if BILLING_ENABLED:
        return HTTPException(status_code=402, detail={
            "error": "no_plan",
            "message": "This action needs an active plan. Choose a plan or add your own API key.",
        })
    return HTTPException(status_code=400, detail="Missing X-Gemini-Key header")


# Probe rate limiter. In-memory, resets on restart by design — the hard monthly
# quota lives in the metering ledger; this only stops someone hammering the
# proxy with metadata probes.
_probe_times: dict = {}  # user_id -> [monotonic timestamps]
PROBES_PER_HOUR = 15

# Out-of-minutes upsell email: at most one per user per day (a client may
# retry the same 402 many times).
_last_quota_email: dict = {}
_QUOTA_EMAIL_COOLDOWN = 24 * 3600


def _maybe_send_quota_email(user):
    if user is None or user.plan != "free" or not user.email:
        return
    now = time.monotonic()
    last = _last_quota_email.get(str(user.id))
    if last is not None and now - last < _QUOTA_EMAIL_COOLDOWN:
        return
    _last_quota_email[str(user.id)] = now
    from cloud.emails import send_out_of_minutes_email
    upgrade_url = f"{_cloud_config.settings.frontend_url}/#/pricing"
    asyncio.create_task(send_out_of_minutes_email(user.email, upgrade_url))


def _check_probe_rate(user_id):
    now = time.monotonic()
    times = _probe_times.setdefault(str(user_id), [])
    times[:] = [t for t in times if now - t < 3600]
    if len(times) >= PROBES_PER_HOUR:
        raise HTTPException(status_code=429,
                            detail="Too many requests this hour. Please slow down.")
    times.append(now)


async def reserve_process_minutes(request, url, input_path, job_id):
    """Meter a managed /api/process request.

    Returns (user_id, priority, reservation_id, plan).

    BYOK / self-host requests don't consume minutes (priority 2, no reservation).
    For a managed (entitled, no BYOK header) request this probes the input
    duration, enforces the per-user concurrent-job limit, and reserves minutes —
    raising 402 (quota) or 429 (too many jobs) as needed.

    NOTE: in cloud mode ``resolve_gemini`` ignores ``X-Gemini-Key`` (paid-only,
    no BYOK), so we must NOT skip metering just because that header is present —
    otherwise a client could send a dummy header and run unlimited managed jobs
    on the operator's key for free. Only skip metering when billing is off.
    """
    if not BILLING_ENABLED:
        return None, 2, None, None
    user = await _user_from_request(request)
    if not managed_keys.has_active_entitlement(user):
        return None, 2, None, None  # shouldn't happen (resolve_gemini would have 402'd)

    priority = _cloud_config.PLAN_PRIORITY.get(user.plan, 1)

    # Per-user simultaneous job cap.
    limit = _cloud_config.PLAN_JOB_LIMIT.get(user.plan, 2)
    active = sum(1 for j in jobs.values()
                 if j.get('user_id') == user.id and j.get('status') in ('queued', 'processing'))
    if active >= limit:
        raise HTTPException(status_code=429,
                            detail="You already have the maximum number of jobs running. Please wait.")

    # Probe rate limit: probing costs a (cheap) proxied metadata call. The
    # 20-minute monthly quota is the real bound on free usage; there is no daily
    # job cap.
    _check_probe_rate(user.id)

    # Probe input duration (blocking → run in a thread).
    loop = asyncio.get_event_loop()
    try:
        if url:
            minutes = await loop.run_in_executor(None, _metering.probe_url_minutes, url)
        else:
            minutes = await loop.run_in_executor(None, _metering.probe_file_minutes, input_path)
    except Exception:
        raise HTTPException(status_code=400,
                            detail="Could not determine the video duration. Try a different source.")
    minutes = max(1, math.ceil(minutes))

    try:
        reservation_id = await _metering.reserve_minutes(user.id, minutes, job_id)
    except _metering.QuotaExceeded as e:
        _maybe_send_quota_email(user)
        raise HTTPException(status_code=402, detail={
            "error": "quota_exceeded",
            "minutes_required": e.required,
            "minutes_remaining": e.remaining,
        })

    return user.id, priority, reservation_id, user.plan


async def reserve_managed_action(request, minutes, job_id, job_type):
    """Reserve quota for a synchronous managed action (e.g. thumbnail image gen).

    Returns a reservation_id to commit/release around the work, or None for
    BYOK / self-host. Raises 402 when the user is out of minutes.
    """
    if not BILLING_ENABLED:
        return None
    user = await _user_from_request(request)
    if not managed_keys.has_active_entitlement(user):
        return None  # BYOK header path (self-host) — not metered
    try:
        return await _metering.reserve_minutes(user.id, minutes, job_id, job_type)
    except _metering.QuotaExceeded as e:
        _maybe_send_quota_email(user)
        raise HTTPException(status_code=402, detail={
            "error": "quota_exceeded",
            "minutes_required": e.required,
            "minutes_remaining": e.remaining,
        })


async def require_managed_entitlement(request):
    """Gate a managed compute endpoint that doesn't resolve a Gemini key itself.

    Some endpoints (subtitle/hook FFmpeg re-encodes, render proxy, the thumbnail
    upload that kicks off a YouTube download + Whisper) do expensive server work
    without ever calling ``resolve_gemini``, so nothing was stopping an anonymous
    or non-entitled caller from driving unbounded compute in cloud mode. In cloud
    mode this rejects them with 402; it's a no-op for self-host (BILLING off).
    """
    if not BILLING_ENABLED:
        return None
    user = await _user_from_request(request)
    if not managed_keys.has_active_entitlement(user):
        raise gemini_missing_error()
    return user


async def _owner_id(request):
    """The authenticated cloud user's id to stamp on a new job/session, or None
    for self-host / BYOK / anonymous (BILLING off → nothing to scope)."""
    if not BILLING_ENABLED:
        return None
    user = await _user_from_request(request)
    return user.id if user else None


async def _assert_job_owner(request, record):
    """Cloud multi-tenant guard: reject unless the caller owns this in-memory
    job/session record.

    No-op for self-host (BILLING off) and for records with no owner stamped
    (BYOK / self-host jobs never set ``user_id``). Returns 404 rather than 403 so
    a non-owner can't even confirm the id exists. UUID ids already make these
    stores hard to enumerate; this closes the gap for a shared/leaked id.
    """
    if not BILLING_ENABLED:
        return
    owner = record.get("user_id") if isinstance(record, dict) else None
    if owner is None:
        return
    user = await _user_from_request(request)
    # Compare as strings: live jobs store a uuid.UUID, but jobs recovered from
    # the .owner sidecar store its string form — UUID != str is always True.
    if user is None or str(user.id) != str(owner):
        raise HTTPException(status_code=404, detail="Not found")

# Application State
# PriorityQueue holds (priority, seq, job_id). Lower priority dispatches first:
# pro=0, starter/creator=1, BYOK/anonymous/self-host=2. The seq counter keeps
# FIFO order within a priority and makes the tuples always comparable. With
# BILLING disabled every job enqueues at priority 2 → plain FIFO as before.
job_queue = asyncio.PriorityQueue()
_job_seq = itertools.count()
jobs: Dict[str, Dict] = {}
thumbnail_sessions: Dict[str, Dict] = {}
publish_jobs: Dict[str, Dict] = {}  # {publish_id: {status, result, error}}
# Semester to limit concurrency to MAX_CONCURRENT_JOBS
concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


def _enqueue_job(job_id: str, priority: int = 2):
    job_queue.put_nowait((priority, next(_job_seq), job_id))

def _relocate_root_job_artifacts(job_id: str, job_output_dir: str) -> bool:
    """
    Backward-compat rescue:
    If main.py accidentally wrote metadata/clips into OUTPUT_DIR root (e.g. output/<jobid>_...),
    move them into output/<job_id>/ so the API can find and serve them.
    """
    try:
        os.makedirs(job_output_dir, exist_ok=True)
        root = OUTPUT_DIR
        pattern = os.path.join(root, f"{job_id}_*_metadata.json")
        meta_candidates = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)
        if not meta_candidates:
            return False

        # Move the newest metadata and its associated clips.
        metadata_path = meta_candidates[0]
        base_name = os.path.basename(metadata_path).replace("_metadata.json", "")

        # Move metadata
        dest_metadata = os.path.join(job_output_dir, os.path.basename(metadata_path))
        if os.path.abspath(metadata_path) != os.path.abspath(dest_metadata):
            shutil.move(metadata_path, dest_metadata)

        # Move any clips that match the same base_name into the job folder
        clip_pattern = os.path.join(root, f"{base_name}_clip_*.mp4")
        for clip_path in glob.glob(clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        # Also move any temp_ clips that might remain
        temp_clip_pattern = os.path.join(root, f"temp_{base_name}_clip_*.mp4")
        for clip_path in glob.glob(temp_clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        return True
    except Exception:
        return False

def _recover_jobs_from_disk():
    """Rebuild completed jobs from OUTPUT_DIR after a restart (issue #46 / #18).

    Jobs live in memory, so a restart used to orphan finished clips that are
    still on disk: the frontend restores the job_id from localStorage but every
    endpoint answers 404 "Job not found". Rebuild a minimal completed record
    for each job directory that has a metadata JSON.
    """
    recovered = 0
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return
    for job_id in entries:
        job_path = os.path.join(OUTPUT_DIR, job_id)
        if not os.path.isdir(job_path) or job_id in jobs:
            continue
        # A live resume manifest means the job was interrupted mid-run. Leave it
        # for _resume_interrupted_jobs (called right after) so a partially-clipped
        # job resumes instead of being surfaced as 'completed' with missing clips.
        if os.path.isfile(os.path.join(job_path, _RESUME_FILE)):
            continue
        json_files = glob.glob(os.path.join(job_path, "*_metadata.json"))
        if not json_files:
            continue
        try:
            with open(json_files[0], 'r') as f:
                data = json.load(f)
            base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
            clips = data.get('shorts', [])
            for i, clip in enumerate(clips):
                if not clip.get('video_url'):
                    clip['video_url'] = f"/videos/{job_id}/{base_name}_clip_{i+1}.mp4"
            owner = None
            owner_path = os.path.join(job_path, ".owner")
            if os.path.exists(owner_path):
                with open(owner_path) as f:
                    raw = f.read().strip()
                owner = int(raw) if raw.isdigit() else (raw or None)
            jobs[job_id] = {
                'status': 'completed',
                'logs': ["♻️ Job recovered from disk after server restart."],
                'output_dir': job_path,
                'user_id': owner,
                'result': {'clips': clips, 'cost_analysis': data.get('cost_analysis')},
            }
            recovered += 1
        except Exception as e:
            print(f"⚠️ Could not recover job {job_id}: {e}")
    if recovered:
        print(f"♻️  Recovered {recovered} completed job(s) from disk.")


# --- Mid-flight job resume (survive a redeploy without losing work) ----------
# A job lives only in memory, so killing the container mid-processing used to
# lose it: the user's clip just stops. We persist a tiny manifest per job and,
# on startup, re-enqueue any that were interrupted — the user sees it resume
# instead of vanish. Bounded by MAX_RESUME_ATTEMPTS so a video that reliably
# crashes the worker can't crashloop the service.
_RESUME_FILE = ".resume.json"
MAX_RESUME_ATTEMPTS = 2


def _write_resume_manifest(job_id, cmd, priority, user_id, reservation_id, watermark):
    try:
        path = os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE)
        with open(path, "w") as f:
            json.dump({
                "cmd": cmd, "priority": priority,
                "user_id": None if user_id is None else str(user_id),
                "reservation_id": reservation_id,
                "watermark": bool(watermark), "attempts": 0,
            }, f)
    except Exception as e:
        print(f"⚠️ Could not write resume manifest for {job_id}: {e}")


def _clear_resume_manifest(job_id):
    """Drop the manifest once a job reaches a terminal state, so it is never
    re-run on a later restart. Only an interrupted (still-running) job keeps it."""
    try:
        os.remove(os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ Could not clear resume manifest for {job_id}: {e}")


def _resume_interrupted_jobs() -> set:
    """Re-enqueue jobs that were mid-processing when the server last stopped.

    A job is "interrupted" iff it still has a .resume.json manifest: the manifest
    is cleared only on a terminal subprocess exit (run_job_wrapper), so its
    survival means the worker was killed mid-run. We resume regardless of whether
    *_metadata.json exists — analysis may have finished but clipping did not. The
    re-run is cheap because transcript, scoring/detail missions, and any finished
    clips all load from their on-disk checkpoints; only the incomplete work runs.

    Returns the set of reservation ids for the resumed jobs, so the caller can
    keep them out of the orphaned-reservation refund. Does NO DB work — the DB
    engine isn't up yet at this point in startup. A poison job (too many
    attempts) is simply not resumed; its reservation is then refunded as a
    normal orphan.
    """
    keep_reservations: set = set()
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return keep_reservations
    resumed = 0
    for job_id in entries:
        job_path = os.path.join(OUTPUT_DIR, job_id)
        manifest_path = os.path.join(job_path, _RESUME_FILE)
        if not os.path.isfile(manifest_path):
            continue
        # NOTE: presence of the manifest — not of *_metadata.json — is the true
        # "was interrupted" signal. run_job_wrapper clears the manifest on any
        # terminal exit, so a manifest that survives means the worker died
        # mid-run. Metadata is written before clips render (main.py), so a crash
        # during clipping leaves metadata + a live manifest: we must resume it,
        # not treat it as done. Re-running is cheap now — transcript, analysis
        # missions, and finished clips all load from their checkpoints.
        try:
            with open(manifest_path) as f:
                m = json.load(f)
        except Exception as e:
            print(f"⚠️ Bad resume manifest for {job_id}: {e}")
            continue

        attempts = int(m.get("attempts", 0)) + 1
        user_id = m.get("user_id")
        reservation_id = m.get("reservation_id")
        if attempts > MAX_RESUME_ATTEMPTS:
            # Poison job: don't resume. Leaving its reservation out of the keep
            # set lets the orphan sweep refund it, and the user can retry by hand.
            print(f"🛑 Job {job_id} exceeded {MAX_RESUME_ATTEMPTS} resume attempts — giving up.")
            _clear_resume_manifest(job_id)
            continue

        # Rebuild env from scratch — the manifest holds no secrets. Managed
        # (cloud) jobs get the server key; self-host falls back to its env key.
        env = os.environ.copy()
        if BILLING_ENABLED and user_id is not None:
            try:
                env["GEMINI_API_KEY"] = managed_keys.gemini_key()
            except Exception:
                pass
        if m.get("watermark"):
            env["WATERMARK"] = "1"
        else:
            env.pop("WATERMARK", None)

        m["attempts"] = attempts
        try:
            with open(manifest_path, "w") as f:
                json.dump(m, f)
        except Exception:
            pass

        jobs[job_id] = {
            'status': 'queued',
            'logs': [f"♻️ Resuming your video after a server update (attempt {attempts})."],
            'cmd': m.get("cmd"),
            'env': env,
            'output_dir': job_path,
            'user_id': None if user_id is None else user_id,
            'reservation_id': reservation_id,
            'watermark': bool(m.get("watermark")),
        }
        if reservation_id:
            keep_reservations.add(str(reservation_id))
        _enqueue_job(job_id, int(m.get("priority", 2)))
        resumed += 1
    if resumed:
        print(f"♻️  Re-enqueued {resumed} interrupted job(s) after restart.")
    return keep_reservations


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _active_input_paths():
    """Absolute paths of source files that queued/processing jobs are using.

    Ownership comes from each job's actual command (the token after -i), which
    is authoritative — unlike parsing the filename prefix, it can't be fooled by
    an assembled-upload id that differs from the job id. Used by both cleanup
    sweeps so an in-use source is never deleted mid-run.
    """
    paths = set()
    for jdata in jobs.values():
        if jdata.get('status') not in ('queued', 'processing'):
            continue
        cmd = jdata.get('cmd') or []
        for i, tok in enumerate(cmd):
            if tok == '-i' and i + 1 < len(cmd):
                paths.add(os.path.abspath(cmd[i + 1]))
    return paths


def _enforce_uploads_size_cap():
    """Delete the oldest source uploads while UPLOAD_DIR is over UPLOADS_MAX_GB.

    Sources are only needed while a job runs (and for the preview afterwards),
    but they're the biggest files on disk — up to MAX_FILE_SIZE_MB each.
    """
    cap = UPLOADS_MAX_GB * 1024 ** 3
    if cap <= 0:
        return
    used = _dir_size(UPLOAD_DIR)
    if used <= cap:
        return
    # Never trim a source an active job is still reading (right up until the
    # final clip is cut). Ownership comes from the job command, not the filename.
    protected = _active_input_paths()
    files = []
    for name in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(p):
            try:
                files.append((os.path.getmtime(p), p, os.path.getsize(p)))
            except OSError:
                pass
    files.sort()
    print(f"🧹 Uploads at {used / 1024**3:.1f} GB (cap {UPLOADS_MAX_GB} GB) — trimming.")
    for _mtime, path, size in files:
        if used <= cap:
            break
        if os.path.abspath(path) in protected:
            continue
        try:
            os.remove(path)
            used -= size
            print(f"🧹 Size cap: removed upload {os.path.basename(path)}")
        except OSError:
            pass


def _enforce_output_size_cap():
    """Delete the oldest job dirs while OUTPUT_DIR is over OUTPUT_MAX_GB."""
    cap = OUTPUT_MAX_GB * 1024 ** 3
    if cap <= 0:
        return
    used = _dir_size(OUTPUT_DIR)
    if used <= cap:
        return
    thumbs = os.path.basename(THUMBNAILS_DIR)
    candidates = []
    for job_id in os.listdir(OUTPUT_DIR):
        if job_id == thumbs:
            continue
        p = os.path.join(OUTPUT_DIR, job_id)
        if os.path.isdir(p):
            try:
                candidates.append((os.path.getmtime(p), p, job_id))
            except OSError:
                pass
    candidates.sort()  # oldest first
    print(f"🧹 Output dir at {used / 1024**3:.1f} GB (cap {OUTPUT_MAX_GB} GB) — trimming.")
    for _mtime, path, job_id in candidates:
        if used <= cap:
            break
        # Never purge jobs that are still queued or processing
        if jobs.get(job_id, {}).get('status') in ('queued', 'processing'):
            continue
        size = _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        jobs.pop(job_id, None)
        used -= size
        print(f"🧹 Size cap: purged {job_id} ({size / 1024**2:.0f} MB)")


async def cleanup_jobs():
    """Background task to remove old jobs and files."""
    import time
    print("🧹 Cleanup task started.")
    while True:
        try:
            await asyncio.sleep(300) # Check every 5 minutes
            now = time.time()
            
            # Simple directory cleanup based on modification time
            # Check OUTPUT_DIR
            for job_id in os.listdir(OUTPUT_DIR):
                # Not a job: the thumbnails dir backs a StaticFiles mount, so
                # deleting it would 500 every /thumbnails request until reboot.
                if job_id == os.path.basename(THUMBNAILS_DIR):
                    continue
                # Never purge jobs that are still queued or processing
                job_rec = jobs.get(job_id, {})
                job_status = job_rec.get('status')
                if job_status in ('queued', 'processing'):
                    continue
                # Nor a completed job whose post-clip batch is still running —
                # this covers autopilot auto-styling, which happens after the
                # job reports 'completed'. Purging mid-batch would rmtree the
                # clips out from under the ffmpeg passes.
                _batch = job_rec.get('batch')
                if _batch is not None and getattr(_batch, 'status', None) == 'running':
                    continue
                # Nor an autopilot child whose parent batch is still active
                # (queued children not yet started have no 'batch' attached).
                _ap_id = job_rec.get('autopilot_batch_id')
                if _ap_id and not autopilot_mod.is_cancelled(_ap_id):
                    _ap = autopilot_mod.autopilot_batches.get(_ap_id)
                    if _ap and _ap.get('status') == 'running':
                        continue
                job_path = os.path.join(OUTPUT_DIR, job_id)
                if os.path.isdir(job_path):
                    if now - os.path.getmtime(job_path) > JOB_RETENTION_SECONDS:
                        print(f"🧹 Purging old job: {job_id}")
                        shutil.rmtree(job_path, ignore_errors=True)
                        if job_id in jobs:
                            del jobs[job_id]

            # Hard disk cap. The time-based sweep above bounds the *age* of what
            # we keep, not its size: a burst of long videos can fill the volume
            # inside one retention window. Drop the oldest jobs until we're back
            # under the cap — clips are already archived to R2 and get restored
            # on demand, so this only costs a re-download.
            _enforce_output_size_cap()
            _enforce_uploads_size_cap()

            # Cleanup SaaSShorts jobs from memory
            try:
                saas_expired = [
                    jid for jid, jdata in list(saas_jobs.items())
                    if jdata.get("status") in ("completed", "failed")
                    and jdata.get("output_dir")
                    and os.path.isdir(jdata["output_dir"])
                    and now - os.path.getmtime(jdata["output_dir"]) > JOB_RETENTION_SECONDS
                ]
                for jid in saas_expired:
                    del saas_jobs[jid]
            except NameError:
                pass

            # Cleanup terminal autopilot batch records from memory (child job
            # dirs are swept by the per-job logic above; this only drops the
            # in-memory parent record once it's done and past retention).
            try:
                autopilot_mod.cleanup_expired(jobs, JOB_RETENTION_SECONDS)
            except Exception:
                pass

            # Cleanup Uploads (skip files belonging to active jobs).
            # Ownership is taken from each active job's actual command (the -i
            # input path), not parsed from the filename — the assembled upload
            # id and the job id don't always match, and a job can outlive the
            # retention window (a long transcription alone can exceed it). An
            # in-use source must never be deleted mid-run regardless of age.
            protected = _active_input_paths()
            for filename in os.listdir(UPLOAD_DIR):
                file_path = os.path.join(UPLOAD_DIR, filename)
                if os.path.abspath(file_path) in protected:
                    continue
                try:
                    if now - os.path.getmtime(file_path) > JOB_RETENTION_SECONDS:
                         os.remove(file_path)
                except Exception: pass

        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")

async def process_queue():
    """Background worker to process jobs from the queue with concurrency limit."""
    print(f"🚀 Job Queue Worker started with {MAX_CONCURRENT_JOBS} concurrent slots.")
    while True:
        try:
            # Wait for a job (priority, seq, job_id) — lowest priority first.
            _priority, _seq, job_id = await job_queue.get()

            # Acquire semaphore slot (waits if max jobs are running)
            await concurrency_semaphore.acquire()
            print(f"🔄 Acquired slot for job: {job_id}")

            # Process in background task to not block the loop (allowing other slots to fill)
            asyncio.create_task(run_job_wrapper(job_id))
            
        except Exception as e:
            print(f"❌ Queue dispatch error: {e}")
            await asyncio.sleep(1)

# Monthly proxy bandwidth counter (in-memory; an alert threshold, not a bill —
# losing it on a deploy just means the alert re-arms from 0 mid-month).
_proxy_month = {"month": None, "bytes": 0, "alerted": False}
PROXY_ALERT_GB = 100


async def _track_proxy_usage(job_id):
    nbytes = (jobs.get(job_id) or {}).get('proxy_bytes') or 0
    if not nbytes:
        return
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if _proxy_month["month"] != month:
        _proxy_month.update(month=month, bytes=0, alerted=False)
    _proxy_month["bytes"] += nbytes
    gb = _proxy_month["bytes"] / 1e9
    if gb >= PROXY_ALERT_GB and not _proxy_month["alerted"] and _alerts:
        _proxy_month["alerted"] = True
        try:
            await _alerts.send_admin_alert(
                "Proxy bandwidth threshold",
                f"Managed downloads have used {gb:.1f} GB of proxy bandwidth in {month} "
                f"(threshold {PROXY_ALERT_GB} GB). Review free-plan usage.",
            )
        except Exception as e:
            print(f"⚠️ Proxy alert failed: {e}")


async def run_job_wrapper(job_id):
    """Wrapper to run job and release semaphore"""
    try:
        job = jobs.get(job_id)
        if job:
            await run_job(job_id, job)
    except Exception as e:
         print(f"❌ Job wrapper error {job_id}: {e}")
    finally:
        # The subprocess returned (success or genuine failure) — a terminal
        # state, so drop the resume manifest. It only survives if the container
        # was killed mid-run, which is exactly when we want to resume.
        _clear_resume_manifest(job_id)
        # Settle the minute reservation (managed jobs only): commit on success,
        # release otherwise so the minutes go back to the user.
        await _settle_reservation(job_id)
        # Archive the completed clips to the user's durable R2 library (history).
        await _archive_managed_job(job_id)
        # Operational alerting for managed jobs (proxy out of credits / failures).
        await _record_job_alert(job_id)
        # Accumulate proxy bandwidth for the monthly cost alert.
        await _track_proxy_usage(job_id)
        # Tell the owner their clips are ready (managed jobs, once per job).
        await _notify_clips_ready(job_id)
        # Telegram pulse for high-signal activity (first clip / paid user).
        await _notify_clip_activity(job_id)
        # Always release semaphore and mark queue task done
        concurrency_semaphore.release()
        job_queue.task_done()
        print(f"✅ Released slot for job: {job_id}")


async def _archive_managed_job(job_id):
    if not BILLING_ENABLED:
        return
    job = jobs.get(job_id) or {}
    if not job.get('user_id') or job.get('status') != 'completed':
        return
    clips = (job.get('result') or {}).get('clips') or []
    if not clips:
        return
    try:
        await cloud.videos.archive_job(job['user_id'], job_id, clips, job['output_dir'])
    except Exception as e:
        print(f"⚠️  R2 archive error for {job_id}: {e}")


def _archive_clip_edit_bg(job_id: str, clip_index: int, filename: str):
    """Fire-and-forget R2 re-archive of an edited clip (managed jobs only).

    Keeps the user's durable library (history/projects) pointing at the current
    version of each clip without blocking the edit response."""
    if not BILLING_ENABLED:
        return
    user_id = (jobs.get(job_id) or {}).get('user_id')
    if not user_id:
        return
    output_dir = os.path.join(OUTPUT_DIR, job_id)

    async def _run():
        try:
            await cloud.videos.archive_clip_edit(user_id, job_id, clip_index, output_dir, filename)
        except Exception as e:
            print(f"⚠️  R2 edit archive error for {job_id}: {e}")

    asyncio.create_task(_run())


def _resolve_clip_for_publishing(job_id: str, clip_index: int):
    """Answer publishing/clips.py's one question: where are this clip's bytes?

    Registered at startup rather than imported, because the job store lives in
    this module's memory and `publishing` must not import `app`.

    Reads the LIVE job record, so a clip that was subtitled/hooked/re-styled
    after generation publishes in its current form — the same reason the edit
    endpoints write `video_url` back into `job['result']['clips']`. Returns None
    when the job or the file is gone (retention sweeps clips after 24 h), which
    the dispatcher treats as a permanent, non-retryable reason.
    """
    if not PUBLISHING_ENABLED:
        return None
    job = jobs.get(job_id) or {}
    clips = (job.get('result') or {}).get('clips') or []
    if clip_index < 0 or clip_index >= len(clips):
        return None
    clip = clips[clip_index]

    info = publish_media.describe_clip(OUTPUT_DIR, job_id, clip_index, clip)
    if not info.get("exists"):
        return None

    title = (clip.get('video_title_for_youtube_short')
             or clip.get('title') or "")
    return {
        # The BASE directory, not the job's — clip_local_path() joins the job id
        # itself, so handing it output/<job_id> would look under it twice.
        "output_dir": OUTPUT_DIR,
        "filename": info["filename"],
        "user_id": job.get('user_id'),
        "title": title,
        "caption": clip.get('video_description_for_instagram') or title,
        "duration": info.get("duration_seconds"),
        "size_bytes": info.get("size_bytes"),
        "mtime": info.get("mtime"),
        "fingerprint": info.get("fingerprint"),
        # The pipeline already writes one text per platform; passing them through
        # is what stops a 3-account fan-out from using a YouTube title as an
        # Instagram caption.
        "per_platform": {
            "youtube": {"title": title, "caption": title},
            "instagram": {
                "caption": clip.get('video_description_for_instagram') or title},
            "tiktok": {
                "caption": clip.get('video_description_for_tiktok') or title},
        },
    }


def _schedule_autopublish(job_id: str, clip_count: int, loop):
    """Hand a finished, styled job to the publishing planner.

    Called from the autopilot styling thread, so the coroutine is scheduled back
    onto the event loop rather than awaited. A no-op unless publishing is enabled
    AND the batch was submitted with a publish plan, which is what keeps every
    existing autopilot batch's behaviour byte-for-byte unchanged.
    """
    if not PUBLISHING_ENABLED or loop is None:
        return
    try:
        plan = autopilot_mod.publish_plan_for_job(job_id)
        if not plan:
            return
        from publishing import autopublish
        user_id = (jobs.get(job_id) or {}).get('user_id')

        async def _run():
            report = await autopublish.publish_job_clips(
                job_id, clip_count, plan, user_id=user_id)
            job = jobs.get(job_id)
            if job is not None:
                n = len(report.get('created') or [])
                skipped = len(report.get('skipped') or [])
                job.setdefault('logs', []).append(
                    f"Publishing: {n} post(s) scheduled"
                    + (f", {skipped} clip(s) skipped." if skipped else "."))

        asyncio.run_coroutine_threadsafe(_run(), loop)
    except Exception as e:
        # Styling finished and the clips are downloadable; a publishing seam
        # failure must not change that.
        print(f"⚠️  Publishing: could not schedule auto-publish for {job_id}: {e}")


def _maybe_autopilot_apply(job_id: str, clip_count: int, loop):
    """Auto-run the batch pipeline on a finished autopilot child job.

    Flag-gated: if the job carries no autopilot_batch_id this is a no-op, so a
    normal job's completion path is byte-for-byte unchanged. Fires the existing
    run_batch in a daemon thread (exactly like start_batch) and does NOT await —
    the run_job_wrapper finally (archive/notify/semaphore) must not be delayed.

    Keys are sourced server-side, not from request headers (there is no request
    here, hours after submit): the Gemini key from the job's own env, the
    ElevenLabs key from the in-memory autopilot batch record. After the batch
    finishes, a durable R2 re-archive is scheduled back on the event loop, since
    run_batch's per-clip archive_fn is a no-op from a worker thread.
    """
    job = jobs.get(job_id) or {}
    batch_id = job.get('autopilot_batch_id')
    if not batch_id:
        return  # not an autopilot child — nothing to do

    # job['autopilot_styling'] tells the board whether a per-clip batch is still
    # coming. Without it, a child that needs no styling would sit at clips_ready
    # forever (that stage is non-terminal), so the batch would never complete.
    if autopilot_mod.is_cancelled(batch_id):
        job['autopilot_styling'] = 'skipped'
        job.setdefault('logs', []).append("Autopilot batch cancelled; skipping auto-styling.")
        return

    output_dir = job.get('output_dir') or os.path.join(OUTPUT_DIR, job_id)
    try:
        default, _overrides = load_options(output_dir)
    except Exception as e:
        # Clips are finished and reviewable; only the styling recipe is lost.
        job['autopilot_styling'] = 'skipped'
        job.setdefault('logs', []).append(
            "Autopilot: styling recipe unreadable; clips left unstyled.")
        print(f"⚠️ autopilot: could not load render options for {job_id}: {e}")
        return

    ap_keys = autopilot_mod.keys_for_job(job_id)
    # auto_edit (editor.py) talks to Gemini directly, whatever the general
    # pipeline runs on. Under an OpenAI-compatible provider the child env has no
    # GEMINI_API_KEY at all, so fall back to the key the batch captured at
    # creation — otherwise auto_edit would fire with api_key=None.
    gemini_key = (job.get('env') or {}).get('GEMINI_API_KEY') or ap_keys.get('gemini')
    elevenlabs_key = ap_keys.get('elevenlabs')
    operations = render_options_to_operations(
        default, gemini_key=gemini_key, elevenlabs_key=elevenlabs_key)
    if not operations:
        # Recipe styled nothing (all modules off) — clips are already final, so
        # this is a legitimate point to publish from. Deliberately NOT done on
        # the recipe-unreadable path above: posting unbranded clips to real
        # accounts is the wrong response to an error.
        job['autopilot_styling'] = 'skipped'
        _schedule_autopublish(job_id, clip_count, loop)
        return

    from batch import run_batch as _run_batch

    def _worker():
        try:
            _run_batch(job_id, list(range(clip_count)), operations, output_dir,
                       jobs, _archive_clip_edit_bg)
        except Exception as e:
            print(f"⚠️ autopilot: auto-batch failed for {job_id}: {e}")
        finally:
            # run_batch's per-clip archive_fn can't schedule tasks from this
            # thread; do the durable R2 re-archive here, on the event loop, so
            # the library reflects the post-styling video_urls.
            if BILLING_ENABLED and loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(_archive_managed_job(job_id), loop)
                except Exception as e:
                    print(f"⚠️ autopilot: re-archive schedule failed for {job_id}: {e}")
            # Auto-publish, if this batch was submitted with a plan. HERE and not
            # in run_job_wrapper's finally: that one runs the moment the clips
            # render, before styling, so it would publish the unstyled cut.
            _schedule_autopublish(job_id, clip_count, loop)

    job['autopilot_styling'] = 'running'
    job.setdefault('logs', []).append(
        f"Autopilot: auto-applying {len(operations)} operation(s) to {clip_count} clip(s).")
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


async def _notify_clips_ready(job_id):
    """Email the owner when their clips finish — processing takes minutes, so
    this lets them close the tab. Once per job (email_sent flag)."""
    if not BILLING_ENABLED:
        return
    job = jobs.get(job_id) or {}
    if not job.get('user_id') or job.get('status') != 'completed' or job.get('email_sent'):
        return
    clips = (job.get('result') or {}).get('clips') or []
    if not clips:
        return
    job['email_sent'] = True
    try:
        from cloud.database import session as cloud_session
        from cloud.models import User
        from cloud.emails import send_clips_ready_email
        async with cloud_session() as s:
            user = await s.get(User, job['user_id'])
        if not user or not user.email:
            return
        title = clips[0].get('video_title_for_youtube_short') or clips[0].get('title') or "Your video"
        await send_clips_ready_email(user.email, title, len(clips),
                                     _cloud_config.settings.frontend_url)
    except Exception as e:
        print(f"⚠️  Clips-ready email error for {job_id}: {e}")


async def _notify_clip_activity(job_id):
    """High-signal Telegram pulse when clips are created — NOT every clip (that
    would drown the ops channel as free usage grows). Only:
      * a user's very first clips (activation), and
      * any paid user's clips.
    Telegram-only (best effort, no email)."""
    if not BILLING_ENABLED:
        return
    job = jobs.get(job_id) or {}
    if not job.get('user_id') or job.get('status') != 'completed':
        return
    clips = (job.get('result') or {}).get('clips') or []
    if not clips:
        return
    try:
        from sqlalchemy import select, func, and_
        from cloud.database import session as cloud_session
        from cloud.models import User, UserVideo
        from cloud import metering
        async with cloud_session() as s:
            user = await s.get(User, job['user_id'])
            if not user:
                return
            sub = await metering._active_subscription(s, user.id)
            # Clips from OTHER jobs (this job's are already archived by now).
            prior = (await s.execute(
                select(func.count(UserVideo.id)).where(and_(
                    UserVideo.user_id == user.id, UserVideo.job_id != job_id,
                ))
            )).scalar_one()
        plan = sub.plan if sub else "free"
        is_paid = sub is not None
        first_clip = prior == 0
        if not (first_clip or is_paid):
            return
        title = clips[0].get('video_title_for_youtube_short') or clips[0].get('title') or "video"
        n = len(clips)
        tag = "🎬 First clips!" if first_clip else "🎬 Clips created"
        await _alerts.send_telegram(
            f"{tag}\n{user.email} ({plan}) — “{title}” ({n} clip{'s' if n != 1 else ''})")
    except Exception as e:
        print(f"⚠️  Clip-activity notify error for {job_id}: {e}")


async def _record_job_alert(job_id):
    if not BILLING_ENABLED:
        return
    job = jobs.get(job_id) or {}
    if not job.get('user_id'):
        return  # only track managed jobs
    ok = job.get('status') == 'completed'
    err = "" if ok else " ".join(job.get('logs', [])[-10:])
    try:
        await _alerts.record_job_outcome(ok, err)
    except Exception as e:
        print(f"⚠️  Alert recording error for {job_id}: {e}")


async def _settle_reservation(job_id):
    if not BILLING_ENABLED:
        return
    job = jobs.get(job_id) or {}
    reservation_id = job.get('reservation_id')
    if not reservation_id:
        return
    try:
        if job.get('status') == 'completed':
            await cloud.metering.commit_reservation(reservation_id)
        else:
            await cloud.metering.release_reservation(reservation_id)
    except Exception as e:
        print(f"⚠️  Reservation settle error for {job_id}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rehydrate finished jobs from disk before serving (survives restarts).
    _recover_jobs_from_disk()
    # Re-enqueue jobs that were mid-processing when we stopped (redeploy). Their
    # reservations must survive the orphan sweep so the resumed run can settle them.
    _resumed_reservation_ids = _resume_interrupted_jobs()
    # Start worker and cleanup
    worker_task = asyncio.create_task(process_queue())
    cleanup_task = asyncio.create_task(cleanup_jobs())
    if BILLING_ENABLED:
        await cloud.setup_async(app, keep_reservation_ids=_resumed_reservation_ids)
    if PUBLISHING_ENABLED:
        # Registered before the loops start so a due attempt claimed on the very
        # first dispatch tick can already resolve its clip.
        publish_clips.set_resolver(_resolve_clip_for_publishing)
        await publishing.setup_async(app)
    yield
    # Cleanup (optional: cancel worker)

app = FastAPI(lifespan=lifespan)

# Cloud mode: attach middleware + routers at import time (before the app serves).
if BILLING_ENABLED:
    cloud.setup_sync(app)

# Publishing routers, likewise at import time. validate_required() runs in here,
# so a misconfigured deploy fails at boot rather than at the first publish.
if PUBLISHING_ENABLED:
    publishing.setup_sync(app)

# Enable CORS for frontend. Cloud mode locks this down to the configured origins;
# self-host keeps the permissive wildcard it has always used.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cloud.settings.allowed_origins if BILLING_ENABLED else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for serving videos
app.mount("/videos", StaticFiles(directory=OUTPUT_DIR), name="videos")

# Mount static files for serving thumbnails
THUMBNAILS_DIR = os.path.join(OUTPUT_DIR, "thumbnails")
os.makedirs(THUMBNAILS_DIR, exist_ok=True)
app.mount("/thumbnails", StaticFiles(directory=THUMBNAILS_DIR), name="thumbnails")


def _safe_under(base_dir: str, user_rel_path: str) -> Optional[str]:
    """Resolve ``user_rel_path`` under ``base_dir`` and reject path traversal.

    Returns the absolute path only if it stays inside ``base_dir`` (after
    following ``..``); otherwise None. Used to sanitize client-supplied file
    references so ``../../.env`` can't escape the output directories.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, user_rel_path))
    if target == base or target.startswith(base + os.sep):
        return target
    return None

class ProcessRequest(BaseModel):
    url: str

# Masks user:password credentials embedded in any URL (e.g. the residential
# proxy URL that yt-dlp echoes in its verbose debug output) before the line is
# ever printed to the server console or stored in the job log.
_CREDENTIAL_URL_RE = re.compile(r'(\w+://)[^:/@\s]+:[^@/\s]+@')


def _scrub_secrets(line: str) -> str:
    line = _CREDENTIAL_URL_RE.sub(r'\1***:***@', line)
    # Provider API keys (Status 200's `rl_*`) can reach a log line through an
    # httpx error string. The patterns come from publishing.crypto so there is
    # one definition of what a provider secret looks like; the list is empty
    # when publishing is off, making this a no-op.
    for pat in _PUBLISH_SECRET_PATTERNS:
        line = pat.sub(lambda m: m.group(0)[:6] + "…redacted", line)
    return line


# Cloud users don't need (and shouldn't see) implementation details: the ingest
# plumbing (proxy / downloader / cookies) OR which AI model powers it, token
# usage and cost. These are dropped from the client view even when the line is
# emoji-prefixed. Never applied under DEBUG_LOGS (local dev sees everything).
_SENSITIVE_LOG_RE = re.compile(
    # Ingest plumbing
    r'proxy|yt[-_ ]?dlp|youtube-?dl|cookie|residential|po[_ ]?token'
    r'|player_client|extractor|\bdownload|descarg'
    # AI model / provider / cost / pipeline internals
    r'|gemini|openai|anthropic|\bflash\b|\bmodel\b|token|thinking'
    r'|\bcost\b|\$\s*[0-9]|scoring window|shortlist',
    re.IGNORECASE,
)


def _visible_logs(logs):
    """Logs to surface to the client.

    Self-host (BILLING off) shows the full pipeline output so people running
    their own instance can debug. Cloud shows a curated whitelist view
    (log_view.friendly_logs): plain progress for normal users — transcription
    percentage, clip counters — with no file paths, model names or pipeline
    internals.

    DEBUG_LOGS=true forces the full output even under billing — for local dev
    where you run in paid mode but still want the raw logs.
    """
    if not BILLING_ENABLED or DEBUG_LOGS:
        return logs
    from log_view import friendly_logs
    return friendly_logs(logs)


def enqueue_output(out, job_id):
    """Reads output from a subprocess and appends it to jobs logs."""
    try:
        for line in iter(out.readline, b''):
            decoded_line = _scrub_secrets(line.decode('utf-8').strip())
            if decoded_line:
                # Internal marker from main.py's downloader, not a log line.
                if decoded_line.startswith("PROXY_BYTES="):
                    try:
                        if job_id in jobs:
                            jobs[job_id]['proxy_bytes'] = int(decoded_line.split("=", 1)[1])
                    except ValueError:
                        pass
                    continue
                print(f"📝 [Job Output] {decoded_line}")
                if job_id in jobs:
                    jobs[job_id]['logs'].append(decoded_line)
    except Exception as e:
        print(f"Error reading output for job {job_id}: {e}")
    finally:
        out.close()

async def run_job(job_id, job_data):
    """Executes the subprocess for a specific job."""
    
    cmd = job_data['cmd']
    env = job_data['env']
    output_dir = job_data['output_dir']
    
    jobs[job_id]['status'] = 'processing'
    jobs[job_id]['logs'].append("Job started by worker.")
    print(f"🎬 [run_job] Executing command for {job_id}: {' '.join(cmd)}")
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr to stdout
            env=env,
            cwd=os.getcwd()
        )
        
        # We need to capture logs in a thread because Popen isn't async
        t_log = threading.Thread(target=enqueue_output, args=(process.stdout, job_id))
        t_log.daemon = True
        t_log.start()
        
        # Async wait for process with incremental updates
        start_wait = time.time()
        while process.poll() is None:
            await asyncio.sleep(2)
            
            # Check for partial results every 2 seconds
            # Look for metadata file
            try:
                json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
                if json_files:
                    target_json = json_files[0]
                    # Read metadata (it might be being written to, so simple try/except or just read)
                    # Use a lock or just robust read? json.load might fail if file is partial.
                    # Usually main.py writes it once at start (based on my review).
                    if os.path.getsize(target_json) > 0:
                        with open(target_json, 'r') as f:
                            data = json.load(f)
                            
                        base_name = os.path.basename(target_json).replace('_metadata.json', '')
                        clips = data.get('shorts', [])
                        cost_analysis = data.get('cost_analysis')
                        
                        # Check which clips actually exist on disk
                        ready_clips = []
                        for i, clip in enumerate(clips):
                             clip_filename = f"{base_name}_clip_{i+1}.mp4"
                             clip_path = os.path.join(output_dir, clip_filename)
                             if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                                 # Checking if file is growing? For now assume if it exists and main.py moves it there, it's done.
                                 # main.py writes to temp_... then moves to final name. So presence means ready!
                                 clip['video_url'] = f"/videos/{job_id}/{clip_filename}"
                                 ready_clips.append(clip)
                        
                        if ready_clips:
                             jobs[job_id]['result'] = {'clips': ready_clips, 'cost_analysis': cost_analysis}
            except Exception as e:
                # Ignore read errors during processing
                pass

        returncode = process.returncode
        
        if returncode == 0:
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['logs'].append("Process finished successfully.")
            
            # Self-host: silent AWS S3 backup. Cloud mode stores to R2 instead
            # (see _archive_managed_job), so skip the redundant/paid AWS upload.
            if not BILLING_ENABLED:
                loop = asyncio.get_event_loop()
                loop.run_in_executor(None, upload_job_artifacts, output_dir, job_id)
            
            # Find result JSON
            json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
            if not json_files:
                # Backward-compat rescue if outputs were written to OUTPUT_DIR root
                if _relocate_root_job_artifacts(job_id, output_dir):
                    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
            if json_files:
                target_json = json_files[0] 
                with open(target_json, 'r') as f:
                    data = json.load(f)
                
                # Enhance result with video URLs. The metadata lists every clip
                # the LLM *planned*; only report the ones whose .mp4 actually
                # rendered to disk. Otherwise a failed cut (e.g. missing source)
                # surfaces as a gray, unplayable card that 404s on /videos/...
                base_name = os.path.basename(target_json).replace('_metadata.json', '')
                clips = data.get('shorts', [])
                cost_analysis = data.get('cost_analysis')

                ready_clips = []
                for i, clip in enumerate(clips):
                     clip_filename = f"{base_name}_clip_{i+1}.mp4"
                     clip_path = os.path.join(output_dir, clip_filename)
                     if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                         clip['video_url'] = f"/videos/{job_id}/{clip_filename}"
                         ready_clips.append(clip)

                if ready_clips:
                    jobs[job_id]['result'] = {'clips': ready_clips, 'cost_analysis': cost_analysis}
                    # Autopilot: if this job is an autopilot child, auto-run the
                    # batch pipeline on its clips using the saved recipe. No-op
                    # (flag-gated) for every normal job. Fire-and-don't-await.
                    try:
                        _maybe_autopilot_apply(job_id, len(ready_clips),
                                               asyncio.get_event_loop())
                    except Exception as e:
                        # Includes the case where the seam never ran (the loop
                        # argument is evaluated first), so mark styling settled
                        # here too or the board would wait on a batch that is
                        # never coming.
                        jobs[job_id]['autopilot_styling'] = 'skipped'
                        jobs[job_id]['logs'].append(
                            _scrub_secrets(f"Autopilot auto-apply skipped: {e}"))
                else:
                    # Exit code was 0 but nothing landed on disk — treat as failure
                    # rather than reporting phantom clips.
                    jobs[job_id]['status'] = 'failed'
                    jobs[job_id]['logs'].append("No clip files were produced.")
            else:
                 jobs[job_id]['status'] = 'failed'
                 jobs[job_id]['logs'].append("No metadata file generated.")
        else:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['logs'].append(_scrub_secrets(f"Process failed with exit code {returncode}"))
            
    except Exception as e:
        jobs[job_id]['status'] = 'failed'
        # Exception text can embed URLs with credentials (e.g. the proxy URL
        # inside a yt-dlp/httpx error) — scrub before it reaches client logs.
        jobs[job_id]['logs'].append(_scrub_secrets(f"Execution error: {str(e)}"))

@app.get("/health")
async def health():
    """Lightweight liveness probe for uptime monitoring / Coolify health checks."""
    return {"status": "ok"}

@app.get("/api/config")
async def get_config():
    return {
        "youtubeUrlEnabled": not DISABLE_YOUTUBE_URL,
        "billingEnabled": BILLING_ENABLED,
        "googleAuthEnabled": bool(BILLING_ENABLED and cloud.settings.google_auth_enabled),
    }

async def _probe_youtube_quality(url: str) -> dict:
    """Run quality_probe.py in a worker thread; {} on any failure (fail-open)."""
    def _run():
        try:
            proc = subprocess.run(
                [sys.executable, QUALITY_PROBE_SCRIPT, "--url", url],
                capture_output=True, timeout=75,
            )
            return json.loads(proc.stdout.decode(errors="replace").strip() or "{}")
        except Exception as e:
            print(f"⚠️ Quality probe failed ({e}); starting job without gate.")
            return {}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# Chunked upload state (in-memory, keyed by upload_id)
_chunked_uploads: Dict[str, Dict] = {}

@app.post("/api/upload/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    chunk: UploadFile = File(...),
):
    """Receive a single chunk of a large file upload."""
    if upload_id not in _chunked_uploads:
        _chunked_uploads[upload_id] = {
            "chunks": {},
            "total_chunks": total_chunks,
            "filename": chunk.filename,
            "started_at": time.time(),
        }

    # Save chunk to disk
    upload_info = _chunked_uploads[upload_id]
    chunk_path = os.path.join(UPLOAD_DIR, f"chunk_{upload_id}_{chunk_index}")

    with open(chunk_path, "wb") as f:
        content = await chunk.read()
        f.write(content)

    upload_info["chunks"][chunk_index] = chunk_path

    received = len(upload_info["chunks"])
    print(f"📦 Chunk {chunk_index + 1}/{total_chunks} received ({received}/{total_chunks} total)")

    return {
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "status": "received",
        "chunks_received": received,
        "chunks_total": total_chunks,
    }


@app.post("/api/upload/complete")
async def complete_upload(
    upload_id: str = Form(...),
    acknowledged: str = Form(...),
    output_format: Optional[str] = Form(None),
    reframe_mode: Optional[str] = Form(None),
    clip_duration_mode: Optional[str] = Form(None),
    branding: Optional[str] = Form(None),
    request: Request = None,
):
    """Assemble chunks and start processing."""
    if upload_id not in _chunked_uploads:
        raise HTTPException(status_code=404, detail="Upload not found")

    upload_info = _chunked_uploads[upload_id]

    if len(upload_info["chunks"]) != upload_info["total_chunks"]:
        raise HTTPException(
            status_code=400,
            detail=f"Missing chunks: {len(upload_info['chunks'])}/{upload_info['total_chunks']}"
        )

    # Assemble chunks into final file.
    # Name the assembled file with the job_id (not the upload_id) so the uploads
    # cleanup sweep — which derives ownership from filename.split('_')[0] — can
    # match it against the set of active jobs and never delete a source file
    # while its job is still processing.
    filename = upload_info["filename"]
    job_id = str(uuid.uuid4())
    final_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")

    print(f"🔧 Assembling {upload_info['total_chunks']} chunks into {filename}...")

    try:
        with open(final_path, "wb") as out_file:
            for i in range(upload_info["total_chunks"]):
                chunk_path = upload_info["chunks"][i]
                with open(chunk_path, "rb") as chunk_file:
                    while content := chunk_file.read(1024 * 1024):  # 1MB at a time
                        out_file.write(content)
                os.remove(chunk_path)  # Clean up chunk

        # Clean up upload tracking
        del _chunked_uploads[upload_id]

        file_size = os.path.getsize(final_path)
        print(f"✅ Upload complete: {file_size / (1024*1024):.1f}MB")

        # Verify file exists and has content
        if not os.path.exists(final_path) or file_size == 0:
            raise HTTPException(status_code=500, detail="File assembly failed - empty file")

        # Now process the assembled file inline (similar to process_endpoint)
        ack_flag = str(acknowledged).lower() in ("1", "true", "yes")
        if not ack_flag:
            raise HTTPException(status_code=400, detail="You must confirm you own the content.")

        # Capture attestation
        client_ip = request.client.host if request.client else "unknown"
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            client_ip = fwd.split(",")[0].strip()
        user_agent = request.headers.get("user-agent", "")
        attestation = {
            "acknowledged": True,
            "ip": client_ip,
            "user_agent": user_agent,
            "timestamp": time.time(),
            "source": "file",
        }

        # job_id was generated above so the assembled upload is named after it.
        job_output_dir = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(job_output_dir, exist_ok=True)

        # Prepare command
        cmd = ["python", "-u", "main.py", "-i", final_path, "-o", job_output_dir]
        if output_format and output_format != "auto":
            cmd.extend(["--format", output_format])
        if reframe_mode in ("track", "general"):
            cmd.extend(["--reframe-mode", reframe_mode])
        if clip_duration_mode == "short":
            cmd.extend(["--clip-duration", clip_duration_mode])

        # Resolve LLM environment
        llm_env = await resolve_llm_env(request)
        if not llm_env:
            raise gemini_missing_error()

        env = os.environ.copy()
        env.update(llm_env)

        print(f"[attestation] job={job_id} ip={attestation['ip']} source=file ack=true")

        # Create job record
        jobs[job_id] = {
            'status': 'queued',
            'logs': [f"Job {job_id} queued."],
            'cmd': cmd,
            'env': env,
            'output_dir': job_output_dir,
            'attestation': attestation,
        }

        _enqueue_job(job_id, 2)  # Default priority

        print(f"✅ Job created: {job_id}")
        return {"job_id": job_id, "status": "queued"}

    except Exception as e:
        print(f"❌ Assembly error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")


async def _create_and_enqueue_job(
    request: Request,
    input_path: Optional[str],
    url: Optional[str],
    ack_flag: bool,
    output_format: str,
    branding_payload: Optional[dict],
    force_low: bool,
    job_id: Optional[str] = None,
    reframe_mode: str = "auto",
    clip_duration_mode: str = "auto",
    autopilot_batch_id: Optional[str] = None
) -> dict:
    """Create a job record, persist owner info, and enqueue for processing.

    autopilot_batch_id (optional): when set, flags the job as an autopilot child.
    After its clips finalize, run_job auto-runs the batch pipeline using the
    recipe already saved to the job's render_options.json. Absent = today's
    behavior, byte-for-byte.
    """
    if not ack_flag:
        raise HTTPException(status_code=400, detail="You must confirm you own the content or have rights to process it.")

    if url and DISABLE_YOUTUBE_URL:
        raise HTTPException(status_code=403, detail="YouTube URL ingest is disabled on this deployment. Please upload a file you own.")

    # Pre-flight quality gate (URLs only)
    if url and not force_low and QUALITY_GATE_MIN_HEIGHT > 0:
        probe = await _probe_youtube_quality(url)
        max_height = int(probe.get("max_height") or 0)
        if 0 < max_height < QUALITY_GATE_MIN_HEIGHT:
            print(f"⚠️ Quality gate: only {max_height}p available for {url} — asking user first.")
            return {
                "needs_confirmation": True,
                "quality_check": {
                    "max_height": max_height,
                    "min_height": QUALITY_GATE_MIN_HEIGHT,
                    "cookies_invalid": bool(probe.get("cookies_invalid")),
                },
            }

    # Attestation
    client_ip = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        client_ip = fwd.split(",")[0].strip()
    user_agent = request.headers.get("user-agent", "")
    attestation = {
        "acknowledged": True,
        "ip": client_ip,
        "user_agent": user_agent,
        "timestamp": time.time(),
        "source": "url" if url else "file",
    }

    # Job ID
    if not job_id:
        job_id = str(uuid.uuid4())

    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    # Branding
    if branding_payload:
        try:
            logo_data = branding_payload.get("logo", {}).pop("logo_image_data", None)
            branding_cfg = BrandingConfig.model_validate(branding_payload)
            if logo_data and logo_data.startswith("data:image"):
                import base64 as b64mod
                header, encoded = logo_data.split(",", 1)
                logo_bytes = b64mod.b64decode(encoded)
                logo_path = os.path.join(job_output_dir, "branding_logo.png")
                with open(logo_path, "wb") as lf:
                    lf.write(logo_bytes)
                branding_cfg.logo.image_path = "branding_logo.png"
                branding_cfg.logo.enabled = True
            render_opts = RenderOptions(branding=branding_cfg)
            save_options(job_output_dir, render_opts, {})
        except Exception as e:
            print(f"⚠️ Invalid render options ignored: {e}")

    # Command setup
    cmd = ["python", "-u", "main.py"]
    env = os.environ.copy()
    llm_env = await resolve_llm_env(request)
    if not llm_env:
        raise gemini_missing_error()
    env.update(llm_env)

    if url:
        cmd.extend(["-u", url])
    else:
        cmd.extend(["-i", input_path])

    cmd.extend(["-o", job_output_dir])
    if output_format and output_format != "auto":
        cmd.extend(["--format", output_format])
    if reframe_mode in ("track", "general"):
        cmd.extend(["--reframe-mode", reframe_mode])
    if clip_duration_mode == "short":
        cmd.extend(["--clip-duration", clip_duration_mode])

    print(f"[attestation] job={job_id} ip={attestation['ip']} source={attestation['source']} ack=true")

    # Metering
    user_id, priority, reservation_id, user_plan = await reserve_process_minutes(request, url, input_path, job_id)
    if user_plan == "free":
        env["WATERMARK"] = "1"

    # Job record
    jobs[job_id] = {
        'status': 'queued',
        'logs': [f"Job {job_id} queued."],
        'cmd': cmd,
        'env': env,
        'output_dir': job_output_dir,
        'attestation': attestation,
        'user_id': user_id,
        'reservation_id': reservation_id,
        'watermark': env.get("WATERMARK") == "1",
        'autopilot_batch_id': autopilot_batch_id,
    }

    if user_id is not None:
        try:
            os.makedirs(job_output_dir, exist_ok=True)
            with open(os.path.join(job_output_dir, ".owner"), "w") as f:
                f.write(str(user_id))
        except Exception as e:
            print(f"⚠️ Could not persist job owner for {job_id}: {e}")

    _write_resume_manifest(job_id, cmd, priority, user_id, reservation_id,
                           watermark=jobs[job_id]['watermark'])

    _enqueue_job(job_id, priority)

    return {"job_id": job_id, "status": "queued"}


@app.post("/api/process")
async def process_endpoint(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    acknowledged: Optional[str] = Form(None),
    output_format: Optional[str] = Form(None),
    reframe_mode: Optional[str] = Form(None),
    clip_duration_mode: Optional[str] = Form(None),
    force_low_quality: Optional[str] = Form(None),
    branding: Optional[str] = Form(None)
):
    source_type = "file" if file else "url"
    print(f"📥 POST /api/process received: {source_type} upload")

    ack_flag = str(acknowledged).lower() in ("1", "true", "yes")
    force_low = str(force_low_quality).lower() in ("1", "true", "yes")

    # Handle JSON body manually for URL payload
    content_type = request.headers.get("content-type", "")
    branding_payload = None
    if "application/json" in content_type:
        body = await request.json()
        url = body.get("url")
        ack_flag = bool(body.get("acknowledged"))
        force_low = bool(body.get("force_low_quality"))
        output_format = body.get("output_format")
        reframe_mode = body.get("reframe_mode")
        clip_duration_mode = body.get("clip_duration_mode")
        branding_payload = body.get("branding")
    elif branding:
        try:
            branding_payload = json.loads(branding)
        except (json.JSONDecodeError, TypeError):
            pass

    # Normalize output format (auto = keep pipeline default).
    if output_format not in ("vertical", "horizontal", "square"):
        output_format = "auto"

    # Normalize reframe mode (auto = per-scene detection).
    if reframe_mode not in ("track", "general"):
        reframe_mode = "auto"

    # Normalize clip duration mode (auto = 15-60s viral range, short = 11-30s).
    if clip_duration_mode != "short":
        clip_duration_mode = "auto"

    if not url and not file:
        raise HTTPException(status_code=400, detail="Must provide URL or File")

    # If it's a URL, go straight to job creation
    if url:
        return await _create_and_enqueue_job(
            request=request,
            input_path=None,
            url=url,
            ack_flag=ack_flag,
            output_format=output_format,
            branding_payload=branding_payload,
            force_low=force_low,
            reframe_mode=reframe_mode,
            clip_duration_mode=clip_duration_mode
        )

    # For file uploads, save the file first
    safe_name = os.path.basename(file.filename or "upload") or "upload"
    job_id = str(uuid.uuid4())
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{safe_name}")

    # Read file in chunks to check size and save
    size = 0
    limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    last_log_size = 0
    log_interval = 50 * 1024 * 1024  # Log every 50MB

    print(f"📥 Receiving upload: {safe_name} (job {job_id})")

    with open(input_path, "wb") as buffer:
        while content := await file.read(1024 * 1024):
            size += len(content)
            if size > limit_bytes:
                os.remove(input_path)
                raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
            buffer.write(content)

            # Log progress every 50MB
            if size - last_log_size >= log_interval:
                print(f"   Uploaded {size / (1024*1024):.0f}MB...")
                last_log_size = size

    print(f"✅ Upload complete: {size / (1024*1024):.1f}MB")

    # Now create the job
    return await _create_and_enqueue_job(
        request=request,
        input_path=input_path,
        url=None,
        ack_flag=ack_flag,
        output_format=output_format,
        branding_payload=branding_payload,
        force_low=force_low,
        job_id=job_id,
        reframe_mode=reframe_mode,
        clip_duration_mode=clip_duration_mode
    )

    # Persist render options so main.py picks them up during clip rendering
    if branding_payload:
        try:
            # Handle base64 logo image embedded in the payload
            logo_data = branding_payload.get("logo", {}).pop("logo_image_data", None)
            branding_cfg = BrandingConfig.model_validate(branding_payload)
            if logo_data and logo_data.startswith("data:image"):
                import base64 as b64mod
                header, encoded = logo_data.split(",", 1)
                logo_bytes = b64mod.b64decode(encoded)
                logo_path = os.path.join(job_output_dir, "branding_logo.png")
                with open(logo_path, "wb") as lf:
                    lf.write(logo_bytes)
                branding_cfg.logo.image_path = "branding_logo.png"
                branding_cfg.logo.enabled = True
            render_opts = RenderOptions(branding=branding_cfg)
            save_options(job_output_dir, render_opts, {})
        except Exception as e:
            print(f"⚠️ Invalid render options ignored: {e}")

    # Prepare Command
    cmd = ["python", "-u", "main.py"] # -u for unbuffered
    env = os.environ.copy()
    env.update(llm_env)  # Inject provider config (gemini or openai_compat)

    input_path = None
    if url:
        cmd.extend(["-u", url])
    else:
        # Save uploaded file with size limit check.
        # basename() strips any path components from the client-supplied
        # filename so a name like "../../main.py" can't escape UPLOAD_DIR.
        safe_name = os.path.basename(file.filename or "upload") or "upload"
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{safe_name}")

        # Read file in chunks to check size
        size = 0
        limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        last_log_size = 0
        log_interval = 50 * 1024 * 1024  # Log every 50MB

        print(f"📥 Receiving upload: {safe_name} (job {job_id})")

        with open(input_path, "wb") as buffer:
            while content := await file.read(1024 * 1024): # Read 1MB chunks
                size += len(content)
                if size > limit_bytes:
                    os.remove(input_path)
                    shutil.rmtree(job_output_dir)
                    raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
                buffer.write(content)

                # Log progress every 50MB
                if size - last_log_size >= log_interval:
                    print(f"   Uploaded {size / (1024*1024):.0f}MB...")
                    last_log_size = size

        print(f"✅ Upload complete: {size / (1024*1024):.1f}MB")

        cmd.extend(["-i", input_path])

    cmd.extend(["-o", job_output_dir])
    if output_format and output_format != "auto":
        cmd.extend(["--format", output_format])

    print(f"[attestation] job={job_id} ip={attestation['ip']} source={attestation['source']} ack=true")

    # Meter + reserve minutes for managed users (no-op for BYOK / self-host).
    user_id, priority, reservation_id, user_plan = await reserve_process_minutes(request, url, input_path, job_id)
    if user_plan == "free":
        # Free-plan clips carry a burned-in watermark (applied by the main.py
        # subprocess after each clip renders).
        env["WATERMARK"] = "1"

    # Enqueue Job
    jobs[job_id] = {
        'status': 'queued',
        'logs': [f"Job {job_id} queued."],
        'cmd': cmd,
        'env': env,
        'output_dir': job_output_dir,
        'attestation': attestation,
        'user_id': user_id,
        'reservation_id': reservation_id,
        'watermark': env.get("WATERMARK") == "1",
    }

    # Persist the owner so recovered jobs keep their multi-tenant guard after a
    # restart (see _recover_jobs_from_disk).
    if user_id is not None:
        try:
            os.makedirs(job_output_dir, exist_ok=True)
            with open(os.path.join(job_output_dir, ".owner"), "w") as f:
                f.write(str(user_id))
        except Exception as e:
            print(f"⚠️ Could not persist job owner for {job_id}: {e}")

    # Resume manifest: enough to re-run this job if the container dies mid-flight
    # (a redeploy). No secrets — the env is rebuilt from os.environ on resume.
    _write_resume_manifest(job_id, cmd, priority, user_id, reservation_id,
                           watermark=jobs[job_id]['watermark'])

    _enqueue_job(job_id, priority)

    return {"job_id": job_id, "status": "queued"}

@app.get("/api/status/{job_id}")
async def get_status(job_id: str, request: Request):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    await _assert_job_owner(request, job)
    return {
        "status": job['status'],
        "logs": _visible_logs(job['logs']),
        "result": job.get('result')
    }


@app.get("/api/source/{job_id}")
async def get_source_video(job_id: str):
    """Stream a job's original source video for the live-analysis preview.

    Uploaded sources are blob URLs in the browser and don't survive a reload,
    so the recovered session points the preview here instead. Unauthenticated
    like the /videos mount — the UUID job_id is the capability.
    """
    matches = [
        f for f in glob.glob(os.path.join(UPLOAD_DIR, f"{job_id}_*"))
        if not os.path.basename(f).startswith("thumb_")
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="Source not found")
    return FileResponse(matches[0], media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download-all")
async def download_all_clips(job_id: str, request: Request):
    """Bundle the current version of every clip of a job into one ZIP."""
    await _ensure_job_files(job_id, request)
    if job_id in jobs:
        await _assert_job_owner(request, jobs[job_id])

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Job not found")

    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)

    # The metadata file on disk never carries video_url — the pipeline doesn't
    # write it, it's injected into the in-memory job record. So prefer the live
    # record (it also tracks edits like subtitled_/hook_ renames) and fall back
    # to the canonical name a job/restore rebuilds, instead of finding nothing.
    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    mem_clips = ((jobs.get(job_id) or {}).get('result') or {}).get('clips') or []

    files = []
    for i, clip in enumerate(data.get('shorts', [])):
        url = None
        if i < len(mem_clips):
            url = (mem_clips[i] or {}).get('video_url')
        url = url or clip.get('video_url')
        filename = os.path.basename(url.split('/')[-1]) if url else f"{base_name}_clip_{i+1}.mp4"
        path = os.path.join(output_dir, filename)
        if filename and os.path.exists(path):
            files.append((i, path))

    if not files:
        raise HTTPException(status_code=404, detail="No clip files found for this job")

    zip_path = os.path.join(output_dir, f"clips_{int(time.time())}.zip")

    def build_zip():
        # Videos are already compressed; store instead of deflate for speed.
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
            for i, path in files:
                zf.write(path, arcname=f"clip_{i + 1:02d}_{os.path.basename(path)}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, build_zip)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"openshorts_clips_{job_id[:8]}.zip",
        background=BackgroundTask(os.remove, zip_path),
    )


# --- Project restore (paid mode) --------------------------------------------
# Re-hydrates an archived project from R2 back into output/{job_id}/ so every
# edit endpoint works on it again. Restored files land with a fresh mtime, so
# the retention clock restarts; re-restoring after a purge is cheap.
_restore_locks: Dict[str, asyncio.Lock] = {}


@app.post("/api/projects/{job_id}/restore")
async def restore_project(job_id: str, request: Request):
    if not BILLING_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    from sqlalchemy import select
    from cloud.auth import get_current_user_required
    from cloud.models import Project
    from cloud import database as cloud_db, storage as cloud_storage

    user = await get_current_user_required(request)
    async with cloud_db.session() as s:
        proj = (await s.execute(
            select(Project).where(Project.job_id == job_id)
        )).scalar_one_or_none()
    if proj is None or str(proj.user_id) != str(user.id):
        raise HTTPException(status_code=404, detail="Project not found")

    # Per-job lock: a double click must not download the project twice.
    lock = _restore_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        job_dir = os.path.join(OUTPUT_DIR, job_id)

        # Idempotent fast path: everything the project needs is already on disk.
        needed = {os.path.basename(proj.metadata_r2_key)}
        for c in (proj.state or {}).get("clips", []):
            for k in ("original_file", "server_file"):
                if c.get(k):
                    needed.add(c[k])
        if os.path.isdir(job_dir) and all(
            os.path.exists(os.path.join(job_dir, f)) for f in needed
        ):
            os.utime(job_dir, None)  # restart the retention clock
        else:
            prefix = cloud_storage.job_key(user.id, job_id, "")
            keys = await asyncio.to_thread(cloud_storage.list_keys, prefix)
            if not keys:
                raise HTTPException(status_code=502,
                                    detail="Project files are no longer available")
            # Download into a temp dir first so a partial failure never leaves a
            # half-restored job dir that the fast path would mistake for complete.
            tmp_dir = job_dir + ".restoring"
            shutil.rmtree(tmp_dir, ignore_errors=True)
            os.makedirs(tmp_dir, exist_ok=True)
            sem = asyncio.Semaphore(3)

            async def _download(key):
                fname = os.path.basename(key)
                if not fname:
                    return
                async with sem:
                    await asyncio.to_thread(
                        cloud_storage.download_file, key, os.path.join(tmp_dir, fname))

            try:
                await asyncio.gather(*(_download(k) for k in keys))
            except Exception as e:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(status_code=502, detail=f"Restore download failed: {e}")
            # Owner sidecar keeps the multi-tenant guard after a server restart.
            with open(os.path.join(tmp_dir, ".owner"), "w") as f:
                f.write(str(user.id))
            if os.path.isdir(job_dir):
                for fname in os.listdir(tmp_dir):
                    shutil.move(os.path.join(tmp_dir, fname), os.path.join(job_dir, fname))
                shutil.rmtree(tmp_dir, ignore_errors=True)
                os.utime(job_dir, None)
            else:
                os.rename(tmp_dir, job_dir)

        # Register (or refresh) the in-memory job — same shape as
        # _recover_jobs_from_disk, so every edit endpoint works unchanged.
        json_files = glob.glob(os.path.join(job_dir, "*_metadata.json"))
        if not json_files:
            raise HTTPException(status_code=502, detail="Project metadata missing")
        with open(json_files[0], 'r') as f:
            data = json.load(f)
        base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
        clips = data.get('shorts', [])
        for i, clip in enumerate(clips):
            if not clip.get('video_url'):
                clip['video_url'] = f"/videos/{job_id}/{base_name}_clip_{i+1}.mp4"
        jobs[job_id] = {
            'status': 'completed',
            'logs': ["♻️ Project restored from your library."],
            'output_dir': job_dir,
            'user_id': str(user.id),
            'result': {'clips': clips, 'cost_analysis': data.get('cost_analysis')},
        }

    return {
        "job_id": job_id,
        "status": "completed",
        "result": jobs[job_id]['result'],
        "project_state": proj.state,
        "title": proj.title,
    }


async def _ensure_job_files(job_id: str, request: Request) -> bool:
    """Make a completed job usable again after its working files vanished.

    OUTPUT_DIR is not durable — a container restart or redeploy wipes it — so
    endpoints that read a job's files would 404 on a project the user can still
    see in their library. Pull it back from R2 on demand (same path as the
    explicit /restore), so editing keeps working instead of dead-ending.

    Returns True when the job is available afterwards. Never raises: callers
    keep their own 404s for jobs that genuinely don't exist.
    """
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    if job_id in jobs and glob.glob(os.path.join(job_dir, "*_metadata.json")):
        return True
    if not BILLING_ENABLED:
        return False
    try:
        await restore_project(job_id, request)
        print(f"♻️  Auto-restored {job_id} from the library (working files were gone).")
        return True
    except HTTPException:
        return False
    except Exception as e:
        print(f"⚠️  Auto-restore failed for {job_id}: {e}")
        return False


from editor import VideoEditor
from subtitles import generate_srt, generate_ass, burn_subtitles, generate_srt_from_video
from hooks import add_hook_to_video
from translate import translate_video, get_supported_languages
from thumbnail import analyze_video_for_titles, refine_titles, generate_thumbnail, generate_youtube_description

class EditRequest(BaseModel):
    job_id: str
    clip_index: int
    api_key: Optional[str] = None
    input_filename: Optional[str] = None

@app.post("/api/edit")
async def edit_clip(
    req: EditRequest,
    request: Request,
):
    # Cloud (paid) mode disables BYOK: ignore any body api_key so it can't skip
    # the entitlement gate or metering (mirrors resolve_gemini ignoring the
    # header). Self-host keeps BYOK — the body key wins there.
    body_key = None if BILLING_ENABLED else req.api_key
    final_api_key = body_key or await resolve_gemini(request)

    if not final_api_key:
        raise gemini_missing_error()

    await _ensure_job_files(req.job_id, request)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    await _assert_job_owner(request, job)
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    # Meter the managed Gemini call so it can't be looped for free. Skip only for
    # genuine BYOK (self-host body key) — in cloud, body_key is always None.
    edit_minutes = _cloud_config.MANAGED_ANALYSIS_MINUTES if BILLING_ENABLED else 0
    reservation_id = None if body_key else await reserve_managed_action(
        request, edit_minutes, req.job_id, "edit")

    try:
        # Resolve Input Path: Prefer explict input_filename from frontend (chaining edits)
        if req.input_filename:
            # Security: Ensure just a filename, no paths
            safe_name = os.path.basename(req.input_filename)
            input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_name)
            filename = safe_name
        else:
            # Fallback to original clip
            clip = job['result']['clips'][req.clip_index]
            filename = clip['video_url'].split('/')[-1]
            input_path = os.path.join(OUTPUT_DIR, req.job_id, filename)
        
        if not os.path.exists(input_path):
             raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

        # Define output path for edited video
        edited_filename = f"edited_{filename}"
        output_path = os.path.join(OUTPUT_DIR, req.job_id, edited_filename)
        
        # Run editing in a thread to avoid blocking main loop
        # Since VideoEditor uses blocking calls (subprocess, API wait)
        def run_edit():
            editor = VideoEditor(api_key=final_api_key)
            
            # SAFE FILE RENAMING STRATEGY (Avoid UnicodeEncodeError in Docker)
            # Create a safe ASCII filename in the same directory
            safe_filename = f"temp_input_{req.job_id}.mp4"
            safe_input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_filename)
            
            # Copy original file to safe path
            # (Copy is safer than rename if something crashes, we keep original)
            shutil.copy(input_path, safe_input_path)
            
            try:
                # 1. Upload (using safe path)
                vid_file = editor.upload_video(safe_input_path)
                
                # 2. Get duration
                import cv2
                cap = cv2.VideoCapture(safe_input_path)
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                duration = frame_count / fps if fps else 0
                cap.release()
                
                # Load transcript from metadata
                transcript = None
                try:
                    meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
                    if meta_files:
                        with open(meta_files[0], 'r') as f:
                            data = json.load(f)
                            transcript = data.get('transcript')
                except Exception as e:
                    print(f"⚠️ Could not load transcript for editing context: {e}")

                # 3. Get Plan (Filter String)
                # Zooms would crop burned-in captions/hooks off screen, so tell
                # the editor when the source already carries them. `filename` is
                # the original clip name (safe_input_path is an ASCII temp copy).
                has_captions = ("subtitled_" in filename) or ("hook_" in filename)
                filter_data = editor.get_ffmpeg_filter(vid_file, duration, fps=fps, width=width, height=height, transcript=transcript, has_captions=has_captions)
                
                # 4. Apply
                # Use safe output name first
                safe_output_path = os.path.join(OUTPUT_DIR, req.job_id, f"temp_output_{req.job_id}.mp4")
                editor.apply_edits(safe_input_path, safe_output_path, filter_data)
                
                # Move result to final destination (rename works even if dest name has unicode if filesystem supports it, 
                # but python might still struggle if locale is broken? No, os.rename usually handles it better than subprocess args)
                # Actually, output_path is defined above: f"edited_{filename}"
                # If filename has unicode, output_path has unicode.
                # Let's hope shutil.move / os.rename works.
                if os.path.exists(safe_output_path):
                    shutil.move(safe_output_path, output_path)
                
                return filter_data
            finally:
                # Cleanup temp safe input
                if os.path.exists(safe_input_path):
                    os.remove(safe_input_path)

        # Run in thread pool
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, run_edit)

        new_video_url = f"/videos/{req.job_id}/{edited_filename}"

        # Persist the new current file like /api/subtitle does: in-memory job
        # result + metadata.json, so reload/recovery/re-archive see this version.
        if req.clip_index < len(job['result']['clips']):
            job['result']['clips'][req.clip_index]['video_url'] = new_video_url
        try:
            meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
            if meta_files:
                with open(meta_files[0], 'r') as f:
                    meta = json.load(f)
                shorts = meta.get('shorts', [])
                if req.clip_index < len(shorts):
                    shorts[req.clip_index]['video_url'] = new_video_url
                    meta['shorts'] = shorts
                    with open(meta_files[0], 'w') as f:
                        json.dump(meta, f, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to update metadata.json: {e}")

        _archive_clip_edit_bg(req.job_id, req.clip_index, edited_filename)

        if reservation_id:
            await _metering.commit_reservation(reservation_id)
        return {
            "success": True,
            "new_video_url": new_video_url,
            "edit_plan": plan
        }

    except Exception as e:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        print(f"❌ Edit Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class SubtitleRequest(BaseModel):
    job_id: str
    clip_index: int
    position: str = "bottom" # top, middle, bottom
    font_size: int = 16
    font_name: str = "Verdana"
    font_color: str = "#FFFFFF"
    border_color: str = "#000000"
    border_width: int = 2
    bg_color: str = "#000000"
    bg_opacity: float = 0.0
    style: str = "classic"  # classic (uniform color) or karaoke (word highlight)
    highlight_color: str = "#FFD700"
    effect: str = "none"  # none | glow | pop | box (karaoke only)
    base_opacity: float = 1.0  # opacity of non-active words (dimmed modern look)
    uppercase: bool = False
    input_filename: Optional[str] = None


@app.get("/api/clip/{job_id}/{clip_index}/transcript")
async def get_clip_transcript(job_id: str, clip_index: int, request: Request):
    """Return word-level captions for a specific clip, formatted for Remotion."""
    await _ensure_job_files(job_id, request)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    await _assert_job_owner(request, jobs[job_id])
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r') as f:
        data = json.load(f)

    transcript = data.get('transcript')
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata")

    clips = data.get('shorts', [])
    if clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_data = clips[clip_index]
    clip_start = clip_data.get('start', 0)
    clip_end = clip_data.get('end', 0)

    # Extract words within clip range and convert to CaptionWord format
    captions = []
    for segment in transcript.get('segments', []):
        for word_info in segment.get('words', []):
            if word_info['end'] > clip_start and word_info['start'] < clip_end:
                captions.append({
                    "text": word_info.get('word', '').strip(),
                    "startMs": int((max(0, word_info['start'] - clip_start)) * 1000),
                    "endMs": int((max(0, word_info['end'] - clip_start)) * 1000),
                })

    duration_sec = clip_end - clip_start

    return {
        "captions": captions,
        "durationSec": duration_sec,
        "language": transcript.get('language', 'en'),
    }


# --- Remotion Render Proxy ---
RENDER_SERVICE_URL = os.getenv("RENDER_SERVICE_URL", "http://renderer:3100")

@app.post("/api/render")
async def proxy_render(request: Request):
    """Proxy render requests to the Node.js Remotion render service."""
    await require_managed_entitlement(request)
    import httpx
    body = await request.json()
    render_minutes = _cloud_config.RENDER_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(
        request, render_minutes, str(uuid.uuid4()), "render")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{RENDER_SERVICE_URL}/render", json=body)
        result = resp.json()
        if reservation_id:
            await _metering.commit_reservation(reservation_id)
        return result
    except Exception as e:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        raise HTTPException(status_code=502, detail=f"Render service unavailable: {e}")

@app.get("/api/render/{render_id}")
async def proxy_render_status(render_id: str):
    """Proxy render status polling to the Node.js Remotion render service."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{RENDER_SERVICE_URL}/render/{render_id}")
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Render service unavailable: {e}")


class EffectsGenerateRequest(BaseModel):
    job_id: str
    clip_index: int
    input_filename: Optional[str] = None

@app.post("/api/effects/generate")
async def generate_effects_config(
    req: EffectsGenerateRequest,
    request: Request,
):
    """Generate structured EffectsConfig JSON for Remotion rendering via Gemini AI."""
    final_api_key = await resolve_gemini(request)

    if not final_api_key:
        raise gemini_missing_error()

    await _ensure_job_files(req.job_id, request)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    await _assert_job_owner(request, job)
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    # Meter the managed Gemini call (no-op for self-host).
    fx_minutes = _cloud_config.MANAGED_ANALYSIS_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(request, fx_minutes, req.job_id, "effects")

    try:
        # Resolve input path
        if req.input_filename:
            safe_name = os.path.basename(req.input_filename)
            input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_name)
        else:
            clip = job['result']['clips'][req.clip_index]
            filename = clip['video_url'].split('/')[-1]
            input_path = os.path.join(OUTPUT_DIR, req.job_id, filename)

        if not os.path.exists(input_path):
            raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

        def run_effects_generation():
            editor = VideoEditor(api_key=final_api_key)

            # Create safe ASCII filename to avoid encoding issues
            safe_filename = f"temp_effects_{req.job_id}.mp4"
            safe_input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_filename)
            shutil.copy(input_path, safe_input_path)

            try:
                # Upload video to Gemini
                vid_file = editor.upload_video(safe_input_path)

                # Get video metadata via ffprobe
                probe_cmd = [
                    'ffprobe', '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=width,height,r_frame_rate,duration',
                    '-show_entries', 'format=duration',
                    '-of', 'json',
                    safe_input_path
                ]
                probe_result = subprocess.check_output(probe_cmd).decode().strip()
                probe_data = json.loads(probe_result)

                stream = probe_data.get('streams', [{}])[0]
                width = int(stream.get('width', 1080))
                height = int(stream.get('height', 1920))

                # Parse fps from r_frame_rate (e.g. "30/1")
                r_frame_rate = stream.get('r_frame_rate', '30/1')
                num, den = r_frame_rate.split('/')
                fps = round(int(num) / int(den), 2)

                # Get duration from stream or format
                duration = float(stream.get('duration', 0))
                if duration == 0:
                    duration = float(probe_data.get('format', {}).get('duration', 0))

                # Load transcript from metadata
                transcript = None
                try:
                    meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
                    if meta_files:
                        with open(meta_files[0], 'r') as f:
                            data = json.load(f)
                            transcript = data.get('transcript')
                except Exception as e:
                    print(f"⚠️ Could not load transcript for effects config: {e}")

                # Generate effects config
                effects_config = editor.get_effects_config(
                    vid_file, duration, fps=fps, width=width, height=height, transcript=transcript
                )

                return effects_config
            finally:
                if os.path.exists(safe_input_path):
                    os.remove(safe_input_path)

        loop = asyncio.get_event_loop()
        effects_config = await loop.run_in_executor(None, run_effects_generation)

        if effects_config is None:
            raise HTTPException(status_code=500, detail="Failed to generate effects config from Gemini")

        if reservation_id:
            await _metering.commit_reservation(reservation_id)
        return {"effects": effects_config}

    except HTTPException:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        raise
    except Exception as e:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        print(f"❌ Effects Generation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subtitle")
async def add_subtitles(req: SubtitleRequest, request: Request):
    await require_managed_entitlement(request)
    await _ensure_job_files(req.job_id, request)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Reload job data from disk just in case metadata was updated
    job = jobs[req.job_id]
    await _assert_job_owner(request, job)

    # We need to access metadata.json to get the transcript
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r') as f:
        data = json.load(f)
        
    transcript = data.get('transcript')
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata. Please process a new video.")
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]
    
    # Video Path
    if req.input_filename:
        # Use chained file
        filename = os.path.basename(req.input_filename)
    else:
        # Fallback to standard naming
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"

    # Re-subtitling must replace previous subtitles instead of burning over
    # them: walk subtitled_<ts>_ prefixes back to the pre-subtitle file.
    while True:
        m = re.match(r'^subtitled_\d+_(.+)$', filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            break
        filename = m.group(1)

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        # Try looking for edited version if url implied it?
        # Just fail if not found.
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    # Define outputs
    generation_id = int(time.time())
    is_karaoke = req.style == "karaoke"
    srt_filename = f"subs_{req.clip_index}_{generation_id}.{'ass' if is_karaoke else 'srt'}"
    srt_path = os.path.join(output_dir, srt_filename)

    # Style options shared by the karaoke ASS generator paths.
    karaoke_opts = dict(
        alignment=req.position, fontsize=req.font_size, font_name=req.font_name,
        font_color=req.font_color, border_color=req.border_color,
        border_width=req.border_width, highlight_color=req.highlight_color,
        bg_color=req.bg_color, bg_opacity=req.bg_opacity,
        effect=req.effect, base_opacity=req.base_opacity, uppercase=req.uppercase,
    )

    # Output video
    # We create a new file "subtitled_..."
    output_filename = f"subtitled_{generation_id}_{filename}"
    output_path = os.path.join(output_dir, output_filename)

    # Meter the FFmpeg re-encode (and any dubbed-video re-transcription) so it
    # can't be looped for free off-quota. No-op for BYOK / self-host.
    subtitle_minutes = _cloud_config.SUBTITLE_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(
        request, subtitle_minutes, req.job_id, "subtitle")

    try:
        # 1. Generate SRT
        # Check if this is a dubbed video - if so, transcribe it fresh
        is_dubbed = filename.startswith("translated_")

        if is_dubbed:
            print(f"🎙️ Dubbed video detected, transcribing audio for subtitles...")
            def run_transcribe_srt():
                if is_karaoke:
                    return generate_srt_from_video(input_path, srt_path, style="karaoke", **karaoke_opts)
                return generate_srt_from_video(input_path, srt_path)

            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, run_transcribe_srt)
        elif is_karaoke:
            success = generate_ass(transcript, clip_data['start'], clip_data['end'], srt_path, **karaoke_opts)
        else:
            success = generate_srt(transcript, clip_data['start'], clip_data['end'], srt_path)

        if not success:
             raise HTTPException(status_code=400, detail="No words found for this clip range.")

        # 2. Burn Subtitles
        # Run in thread pool
        def run_burn():
             burn_subtitles(input_path, srt_path, output_path,
                           alignment=req.position, fontsize=req.font_size,
                           font_name=req.font_name, font_color=req.font_color,
                           border_color=req.border_color, border_width=req.border_width,
                           bg_color=req.bg_color, bg_opacity=req.bg_opacity)
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_burn)
        
    except Exception as e:
        print(f"❌ Subtitle Error: {e}")
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        raise HTTPException(status_code=500, detail=str(e))

    if reservation_id:
        await _metering.commit_reservation(reservation_id)

    # 3. Update Result and Metadata
    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
         job['result']['clips'][req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
    
    # Update Metadata on Disk (Persistence)
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            # Update the main data structure
            data['shorts'] = clips
            
            # Write back
            with open(json_files[0], 'w') as f:
                json.dump(data, f, indent=4)
                print(f"✅ Metadata updated with subtitled video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")
        # Non-critical, but good for persistence

    _archive_clip_edit_bg(req.job_id, req.clip_index, output_filename)

    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}"
    }

class HookRequest(BaseModel):
    job_id: str
    clip_index: int
    text: str
    input_filename: Optional[str] = None
    position: Optional[str] = "top" # top, center, bottom
    size: Optional[str] = "M" # S, M, L
    duration_seconds: Optional[float] = None  # None = hook visible for the whole clip
    style: Optional[str] = "classic"  # classic/dark/yellow/red/outline/outline_yellow

@app.post("/api/hook")
async def add_hook(req: HookRequest, request: Request):
    await require_managed_entitlement(request)
    await _ensure_job_files(req.job_id, request)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r') as f:
        data = json.load(f)
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]
    
    # Video Path
    if req.input_filename:
        filename = os.path.basename(req.input_filename)
    else:
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"

    # Re-applying a hook should REPLACE the previous one, not stack a second
    # overlay. Walk back leading hook_ prefixes to the pre-hook file (only while
    # that base still exists on disk), mirroring the subtitle walk-back.
    while True:
        m = re.match(r'^hook_(.+)$', filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            break
        filename = m.group(1)

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")
        
    # Output video
    output_filename = f"hook_{filename}"
    output_path = os.path.join(output_dir, output_filename)
    
    # Map Size to Scale
    size_map = {"S": 0.8, "M": 1.0, "L": 1.3}
    font_scale = size_map.get(req.size, 1.0)

    # Meter the FFmpeg overlay re-encode (no-op for BYOK / self-host).
    hook_minutes = _cloud_config.HOOK_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(
        request, hook_minutes, req.job_id, "hook")

    try:
        # Run in thread pool
        def run_hook():
             add_hook_to_video(input_path, req.text, output_path, position=req.position, font_scale=font_scale, duration=req.duration_seconds, style=req.style)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_hook)

    except Exception as e:
        print(f"❌ Hook Error: {e}")
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        raise HTTPException(status_code=500, detail=str(e))

    if reservation_id:
        await _metering.commit_reservation(reservation_id)

    # Update Persistence (Same logic as subtitles)
    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
         job['result']['clips'][req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
    
    # Update Metadata on Disk
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            data['shorts'] = clips
            with open(json_files[0], 'w') as f:
                json.dump(data, f, indent=4)
                print(f"✅ Metadata updated with hook video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")

    _archive_clip_edit_bg(req.job_id, req.clip_index, output_filename)

    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}"
    }


# ---- Render Options endpoints ---------------------------------------------------

class RenderOptionsUpdateRequest(BaseModel):
    scope: str = "clip"  # "clip" | "all"
    clip_index: Optional[int] = None
    changes: dict = {}  # sparse override fields, e.g. {"branding": {"logo": {"position": "top_left"}}}


@app.post("/api/jobs/{job_id}/render-options/logo")
async def upload_render_logo(job_id: str, request: Request, file: UploadFile = File(...)):
    """Upload a PNG logo for the job's render options."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(output_dir, exist_ok=True)

    if not file.filename or not file.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise HTTPException(status_code=400, detail="Logo must be a PNG, JPG, or WebP image")

    logo_filename = "branding_logo.png"
    logo_path = os.path.join(output_dir, logo_filename)
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo file too large (max 10MB)")
    with open(logo_path, "wb") as f:
        f.write(content)

    default, overrides = load_options(output_dir)
    default.branding.logo.image_path = logo_filename
    default.branding.logo.enabled = True
    save_options(output_dir, default, overrides)

    return {"success": True, "logo_path": f"/videos/{job_id}/{logo_filename}"}


@app.get("/api/jobs/{job_id}/render-options")
async def get_render_options(job_id: str, request: Request):
    """Get the job's full render options and per-clip overrides."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    default, overrides = load_options(output_dir)
    return {
        "default": default.model_dump(),
        "clip_overrides": overrides,
    }


@app.put("/api/jobs/{job_id}/render-options")
async def update_render_options(job_id: str, body: RenderOptionsUpdateRequest, request: Request):
    """Update render options.

    scope="clip": sets a sparse override for clip_index, re-renders that clip.
    scope="all": updates the job default, re-renders all non-overridden clips.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    default, overrides = load_options(output_dir)

    if body.scope == "clip":
        if body.clip_index is None:
            raise HTTPException(status_code=400, detail="clip_index required for scope=clip")
        existing = overrides.get(str(body.clip_index), {})
        deep_merge(existing, body.changes)
        overrides[str(body.clip_index)] = existing
        save_options(output_dir, default, overrides)

        def _rebrand_one():
            _rebrand_single_clip(output_dir, job_id, body.clip_index, default, overrides)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _rebrand_one)

        new_filename = _clip_filename_for_index(output_dir, body.clip_index)
        _update_clip_video_url(job, job_id, body.clip_index, new_filename)

        return {"success": True, "scope": "clip", "clip_index": body.clip_index}

    elif body.scope == "all":
        base = default.model_dump()
        deep_merge(base, body.changes)
        default = RenderOptions.model_validate(base)
        save_options(output_dir, default, overrides)

        clips_to_rebrand = []
        json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        num_clips = 0
        if json_files:
            with open(json_files[0], "r") as f:
                meta = json.load(f)
            num_clips = len(meta.get("shorts", []))

        for i in range(num_clips):
            if str(i) not in overrides:
                clips_to_rebrand.append(i)

        def _rebrand_bulk():
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = max(int(os.environ.get("CLIP_WORKERS", "3")), 1)
            with ThreadPoolExecutor(max_workers=min(workers, max(1, len(clips_to_rebrand)))) as pool:
                futures = {
                    pool.submit(_rebrand_single_clip, output_dir, job_id, i, default, overrides): i
                    for i in clips_to_rebrand
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        print(f"   ⚠️ Re-brand clip {futures[future]} failed: {e}")

        if clips_to_rebrand:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _rebrand_bulk)

        for i in clips_to_rebrand:
            new_filename = _clip_filename_for_index(output_dir, i)
            _update_clip_video_url(job, job_id, i, new_filename)

        return {
            "success": True,
            "scope": "all",
            "updated_clips": clips_to_rebrand,
            "skipped_clips": [int(k) for k in overrides.keys()],
        }

    else:
        raise HTTPException(status_code=400, detail="scope must be 'clip' or 'all'")


@app.delete("/api/jobs/{job_id}/render-options/overrides/{clip_index}")
async def delete_render_override(job_id: str, clip_index: int, request: Request):
    """Remove a per-clip override; clip reverts to inheriting the job default."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    default, overrides = load_options(output_dir)

    if str(clip_index) in overrides:
        del overrides[str(clip_index)]
        save_options(output_dir, default, overrides)

        def _rebrand():
            _rebrand_single_clip(output_dir, job_id, clip_index, default, overrides)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _rebrand)

        new_filename = _clip_filename_for_index(output_dir, clip_index)
        _update_clip_video_url(job, job_id, clip_index, new_filename)

    return {"success": True, "clip_index": clip_index}


# ---- Legacy branding endpoints (aliases for backward compat) --------------------

@app.post("/api/jobs/{job_id}/branding/logo")
async def upload_branding_logo_legacy(job_id: str, request: Request, file: UploadFile = File(...)):
    return await upload_render_logo(job_id, request, file)


@app.get("/api/jobs/{job_id}/branding")
async def get_branding_legacy(job_id: str, request: Request):
    """Legacy: returns branding subset of render options."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    default, overrides = load_options(output_dir)
    return {
        "default": default.branding.model_dump(),
        "clip_overrides": overrides,
    }


@app.put("/api/jobs/{job_id}/branding")
async def update_branding_legacy(job_id: str, body: RenderOptionsUpdateRequest, request: Request):
    """Legacy: wraps changes in branding namespace and delegates to render-options."""
    # Wrap bare branding changes (e.g. {"logo": {...}}) into {"branding": {"logo": {...}}}
    wrapped_changes = body.changes
    if "logo" in wrapped_changes and "branding" not in wrapped_changes:
        wrapped_changes = {"branding": wrapped_changes}
    wrapped = RenderOptionsUpdateRequest(scope=body.scope, clip_index=body.clip_index, changes=wrapped_changes)
    return await update_render_options(job_id, wrapped, request)


@app.delete("/api/jobs/{job_id}/branding/overrides/{clip_index}")
async def delete_branding_override_legacy(job_id: str, clip_index: int, request: Request):
    return await delete_render_override(job_id, clip_index, request)


# ---- Batch Processing Pipeline ---------------------------------------------------

from batch import OPERATIONS as BATCH_OPERATIONS, run_batch, BatchProgress

class BatchOperation(BaseModel):
    type: str
    config: dict = {}

class BatchRequest(BaseModel):
    operations: List[BatchOperation]
    clip_indices: Optional[List[int]] = None  # None = all clips

@app.post("/api/jobs/{job_id}/batch")
async def start_batch(job_id: str, req: BatchRequest, request: Request):
    """Start a generic batch processing pipeline over clips."""
    await _ensure_job_files(job_id, request)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    await _assert_job_owner(request, job)

    if "result" not in job or "clips" not in job["result"]:
        raise HTTPException(status_code=400, detail="Job has no clips")

    # Reject if a batch is already running
    existing = job.get("batch")
    if existing and isinstance(existing, BatchProgress) and existing.status == "running":
        raise HTTPException(status_code=409, detail="A batch is already running for this job")

    # Validate operation types
    for op in req.operations:
        if op.type not in BATCH_OPERATIONS:
            raise HTTPException(status_code=400, detail=f"Unknown operation type: {op.type}")

    if not req.operations:
        raise HTTPException(status_code=400, detail="No operations specified")

    total_clips = len(job["result"]["clips"])
    clip_indices = req.clip_indices if req.clip_indices is not None else list(range(total_clips))

    # Validate indices
    for idx in clip_indices:
        if idx < 0 or idx >= total_clips:
            raise HTTPException(status_code=400, detail=f"Clip index {idx} out of range (0-{total_clips-1})")

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    operations = [{"type": op.type, "config": op.config} for op in req.operations]

    # Resolve API keys from headers for operations that need them
    gemini_key = await resolve_gemini(request)
    elevenlabs_key = request.headers.get("x-elevenlabs-key")

    for op in operations:
        if op["type"] == "auto_edit" and not op["config"].get("api_key"):
            op["config"]["api_key"] = gemini_key
        if op["type"] == "translate" and not op["config"].get("api_key"):
            op["config"]["api_key"] = elevenlabs_key

    # Launch background thread
    t = threading.Thread(
        target=run_batch,
        args=(job_id, clip_indices, operations, output_dir, jobs, _archive_clip_edit_bg),
        daemon=True,
    )
    t.start()

    return {"success": True, "total_clips": len(clip_indices), "operations": [op["type"] for op in operations]}


@app.get("/api/jobs/{job_id}/batch")
async def get_batch_progress(job_id: str, request: Request):
    """Get current batch processing progress."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    await _assert_job_owner(request, jobs[job_id])

    batch = jobs[job_id].get("batch")
    if not batch or not isinstance(batch, BatchProgress):
        return {"status": "idle"}
    return batch.to_dict()


@app.post("/api/jobs/{job_id}/batch/cancel")
async def cancel_batch(job_id: str, request: Request):
    """Cancel a running batch. In-flight clips finish their current step."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    await _assert_job_owner(request, jobs[job_id])

    batch = jobs[job_id].get("batch")
    if not batch or not isinstance(batch, BatchProgress):
        raise HTTPException(status_code=404, detail="No batch running")
    if batch.status != "running":
        raise HTTPException(status_code=400, detail=f"Batch is not running (status: {batch.status})")

    batch.cancel()
    return {"success": True, "message": "Cancellation requested"}


# ---- Shared helpers -------------------------------------------------------------

def _clip_filename_for_index(output_dir: str, clip_index: int) -> str:
    """Find the clip filename for a given index by looking at metadata."""
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if json_files:
        with open(json_files[0], "r") as f:
            meta = json.load(f)
        base_name = os.path.basename(json_files[0]).replace("_metadata.json", "")
        return f"{base_name}_clip_{clip_index + 1}.mp4"
    return f"clip_{clip_index + 1}.mp4"


def _rebrand_single_clip(output_dir: str, job_id: str, clip_index: int,
                         default: RenderOptions, overrides: dict):
    """Re-render branding for one clip from its pre-brand intermediate."""
    clip_filename = _clip_filename_for_index(output_dir, clip_index)
    clip_path = os.path.join(output_dir, clip_filename)
    pre_brand_path = clip_path.replace(".mp4", "_pre_brand.mp4")

    clip_override = overrides.get(str(clip_index))
    effective = resolve_options(default, clip_override)

    if not os.path.exists(pre_brand_path):
        apply_branding(clip_path, effective.branding, output_dir)
        return

    rebrand_clip(pre_brand_path, clip_path, effective.branding, output_dir)


def _update_clip_video_url(job: dict, job_id: str, clip_index: int, filename: str):
    """Update in-memory job result and on-disk metadata with new video URL."""
    new_url = f"/videos/{job_id}/{filename}"
    if job.get("result") and clip_index < len(job["result"].get("clips", [])):
        job["result"]["clips"][clip_index]["video_url"] = new_url

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if json_files:
        try:
            with open(json_files[0], "r") as f:
                data = json.load(f)
            clips = data.get("shorts", [])
            if clip_index < len(clips):
                clips[clip_index]["video_url"] = new_url
                data["shorts"] = clips
                with open(json_files[0], "w") as f:
                    json.dump(data, f, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to update metadata for render options: {e}")


class TranslateRequest(BaseModel):
    job_id: str
    clip_index: int
    target_language: str
    source_language: Optional[str] = None
    input_filename: Optional[str] = None

@app.get("/api/translate/languages")
async def get_languages():
    """Return supported languages for translation."""
    return {"languages": get_supported_languages()}

@app.post("/api/translate")
async def translate_clip(
    req: TranslateRequest,
    request: Request,
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key")
):
    """Translate a video clip to a different language using ElevenLabs dubbing."""
    if not x_elevenlabs_key:
        raise HTTPException(status_code=400, detail="Missing X-ElevenLabs-Key header")

    await _ensure_job_files(req.job_id, request)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    await _assert_job_owner(request, job)
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_data = clips[req.clip_index]

    # Video Path
    if req.input_filename:
        filename = os.path.basename(req.input_filename)
    else:
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    # Output video with language suffix
    base, ext = os.path.splitext(filename)
    output_filename = f"translated_{req.target_language}_{base}{ext}"
    output_path = os.path.join(output_dir, output_filename)

    try:
        # Run translation in thread pool (blocking API calls)
        def run_translate():
            return translate_video(
                video_path=input_path,
                output_path=output_path,
                target_language=req.target_language,
                api_key=x_elevenlabs_key,
                source_language=req.source_language,
            )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_translate)

    except Exception as e:
        print(f"❌ Translation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
         job['result']['clips'][req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"

    # Update Metadata on Disk
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            data['shorts'] = clips
            with open(json_files[0], 'w') as f:
                json.dump(data, f, indent=4)
                print(f"✅ Metadata updated with translated video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")

    _archive_clip_edit_bg(req.job_id, req.clip_index, output_filename)

    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}"
    }

class SocialPostRequest(BaseModel):
    job_id: str
    clip_index: int
    api_key: Optional[str] = None  # BYOK; ignored for managed users
    user_id: Optional[str] = None  # BYOK profile; ignored for managed users
    platforms: List[str] # ["tiktok", "instagram", "youtube"]
    # Optional overrides if frontend wants to edit them
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_date: Optional[str] = None # ISO-8601 string
    timezone: Optional[str] = "UTC"

import httpx

@app.post("/api/social/post")
async def post_to_socials(req: SocialPostRequest, request: Request):
    await _ensure_job_files(req.job_id, request)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    # Resolve the Upload-Post key + profile. For managed users the server key is
    # used and their own profile is forced (body api_key / user_id are ignored).
    upload_key, forced_profile = await resolve_upload_post(request, req.api_key)
    if not upload_key:
        raise HTTPException(status_code=400, detail="Missing Upload-Post API key")
    post_user = forced_profile or req.user_id
    if not post_user:
        raise HTTPException(status_code=400, detail="Missing Upload-Post user profile")

    job = jobs[req.job_id]
    await _assert_job_owner(request, job)
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    try:
        clip = job['result']['clips'][req.clip_index]
        # Video URL is relative /videos/..., we need absolute file path
        # clip['video_url'] is like "/videos/{job_id}/{filename}"
        # We constructed it as: f"/videos/{job_id}/{clip_filename}"
        # And file is at f"{OUTPUT_DIR}/{job_id}/{clip_filename}"
        
        filename = clip['video_url'].split('/')[-1]
        file_path = os.path.join(OUTPUT_DIR, req.job_id, filename)
        
        if not os.path.exists(file_path):
             raise HTTPException(status_code=404, detail=f"Video file not found: {file_path}")

        # Construct parameters for Upload-Post API
        # Fallbacks
        final_title = req.title or clip.get('title', 'Viral Short')
        final_description = req.description or clip.get('video_description_for_instagram') or clip.get('video_description_for_tiktok') or "Check this out!"
        
        # Prepare form data
        url = "https://api.upload-post.com/api/upload"
        headers = {
            "Authorization": f"Apikey {upload_key}"
        }

        # Prepare data as dict (httpx handles lists for multiple values)
        data_payload = {
            "user": post_user,
            "title": final_title,
            "platform[]": req.platforms, # Pass list directly
            "async_upload": "true",  # Enable async upload
            # Every platform posts PUBLIC. TikTok's default is "moi
            # uniquement" (private only-me), which leaves the clip stranded
            # private and the provider stuck reporting "processing" — the
            # platform never confirms a private post the same way.
            "privacyStatus": "public"
        }

        # Add scheduling if present
        if req.scheduled_date:
            data_payload["scheduled_date"] = req.scheduled_date
            if req.timezone:
                data_payload["timezone"] = req.timezone
        
        # Add Platform specifics
        if "tiktok" in req.platforms:
             data_payload["tiktok_title"] = final_description
             
        if "instagram" in req.platforms:
             data_payload["instagram_title"] = final_description
             data_payload["media_type"] = "REELS"

        if "youtube" in req.platforms:
             yt_title = req.title or clip.get('video_title_for_youtube_short', final_title)
             data_payload["youtube_title"] = yt_title
             data_payload["youtube_description"] = final_description
             data_payload["privacyStatus"] = "public"

        # Send File
        # httpx AsyncClient requires async file reading or bytes. 
        # Since we have MAX_FILE_SIZE_MB, reading into memory is safe-ish.
        with open(file_path, "rb") as f:
            file_content = f.read()
            
        files = {
            "video": (filename, file_content, "video/mp4")
        }

        # Switch to synchronous Client to avoid "sync request with AsyncClient" error with multipart/files
        with httpx.Client(timeout=120.0) as client:
            print(f"📡 Sending to Upload-Post for platforms: {req.platforms}")
            response = client.post(url, headers=headers, data=data_payload, files=files)
            
        if response.status_code not in [200, 201, 202]: # Added 201
             print(f"❌ Upload-Post Error: {response.text}")
             raise HTTPException(status_code=response.status_code, detail=f"Vendor API Error: {response.text}")

        return response.json()

    except Exception as e:
        print(f"❌ Social Post Exception: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/social/user")
async def get_social_user(request: Request):
    """Proxy to fetch user profiles from Upload-Post.

    BYOK: uses the caller's key and returns all profiles on that account.
    Managed: uses the server key but returns ONLY the caller's own profile.
    """
    api_key, forced_profile = await resolve_upload_post(request, None)
    if not api_key:
         raise HTTPException(status_code=400, detail="Missing X-Upload-Post-Key header")

    url = "https://api.upload-post.com/api/uploadposts/users"
    print(f"🔍 Fetching User ID from: {url}")
    headers = {"Authorization": f"Apikey {api_key}"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"❌ Upload-Post User Fetch Error: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch user: {resp.text}")
            
            data = resp.json()
            print(f"🔍 Upload-Post User Response: {data}")
            
            user_id = None
            # The structure is {'success': True, 'profiles': [{'username': '...'}, ...]}
            profiles_list = []
            if isinstance(data, dict):
                 raw_profiles = data.get('profiles', [])
                 if isinstance(raw_profiles, list):
                     for p in raw_profiles:
                         username = p.get('username')
                         if username:
                             # Determine connected platforms
                             socials = p.get('social_accounts', {})
                             connected = []
                             # Check typical platforms
                             for platform in ['tiktok', 'instagram', 'youtube']:
                                 account_info = socials.get(platform)
                                 # If it's a dict and typically has data, or just not empty string
                                 if isinstance(account_info, dict):
                                     connected.append(platform)
                             
                             profiles_list.append({
                                 "username": username,
                                 "connected": connected
                             })
            
            # Managed users must only ever see their own profile.
            if forced_profile is not None:
                profiles_list = [p for p in profiles_list if p.get("username") == forced_profile]

            if not profiles_list:
                # Fallback if no profiles found
                return {"profiles": [], "error": "No profiles found"}

            return {"profiles": profiles_list}
            
            
        except Exception as e:
             raise HTTPException(status_code=500, detail=str(e))

# --- Thumbnail Studio Endpoints ---

@app.post("/api/thumbnail/upload")
async def thumbnail_upload(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
):
    """Upload video and start background Whisper transcription immediately."""
    await require_managed_entitlement(request)
    if not url and not file:
        raise HTTPException(status_code=400, detail="Must provide URL or File")

    session_id = str(uuid.uuid4())
    transcript_event = asyncio.Event()

    # Save file if uploaded directly. basename() stops a "../../x" filename from
    # escaping UPLOAD_DIR; the chunked read caps memory so a huge body can't OOM.
    video_path = None
    if file:
        safe_name = os.path.basename(file.filename or "upload") or "upload"
        video_path = os.path.join(UPLOAD_DIR, f"thumb_{session_id}_{safe_name}")
        size = 0
        limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        with open(video_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit_bytes:
                    os.remove(video_path)
                    raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
                buffer.write(chunk)

    # Meter a fixed guard cost for the background download + Whisper transcription
    # so an entitled user can't loop it for free. Settled when the job finishes.
    transcribe_minutes = _cloud_config.TRANSCRIBE_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(
        request, transcribe_minutes, session_id, "thumbnail_transcribe")

    # Initialize session
    thumbnail_sessions[session_id] = {
        "user_id": await _owner_id(request),
        "video_path": video_path,
        "transcript_event": transcript_event,
        "transcript_ready": False,
        "transcript": None,
        "transcript_segments": [],
        "video_duration": 0,
        "language": "en",
        "context": "",
        "titles": [],
        "conversation": [],
        "_url": url,  # Store URL for deferred download
    }

    async def run_background_whisper():
        try:
            vpath = video_path
            # Download YouTube video if URL was provided
            if not vpath and url:
                from main import download_youtube_video
                loop = asyncio.get_event_loop()
                vpath, _ = await loop.run_in_executor(None, download_youtube_video, url, UPLOAD_DIR)
                thumbnail_sessions[session_id]["video_path"] = vpath

            from main import transcribe_video
            loop = asyncio.get_event_loop()
            transcript = await loop.run_in_executor(None, transcribe_video, vpath)
            segments = transcript.get("segments", [])
            duration = segments[-1]["end"] if segments else 0

            thumbnail_sessions[session_id].update({
                "transcript_ready": True,
                "transcript": transcript,
                "transcript_segments": segments,
                "video_duration": duration,
                "language": transcript.get("language", "en"),
            })
            print(f"✅ [Thumbnail] Background Whisper complete for session {session_id}")
            if reservation_id:
                await _metering.commit_reservation(reservation_id)
        except Exception as e:
            print(f"❌ [Thumbnail] Background Whisper failed: {e}")
            thumbnail_sessions[session_id]["transcript_error"] = str(e)
            if reservation_id:
                await _metering.release_reservation(reservation_id)
        finally:
            transcript_event.set()

    asyncio.create_task(run_background_whisper())

    return {"session_id": session_id}


@app.post("/api/thumbnail/analyze")
async def thumbnail_analyze(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key")
):
    """Analyze a video and suggest viral YouTube titles."""
    api_key = await resolve_gemini(request)
    if not api_key:
        raise gemini_missing_error()

    pre_transcript = None

    # Check for pre-existing session with background Whisper
    if session_id and session_id in thumbnail_sessions:
        session = thumbnail_sessions[session_id]
        await _assert_job_owner(request, session)

        # Wait for background Whisper to complete
        transcript_event = session.get("transcript_event")
        if transcript_event:
            print(f"⏳ [Thumbnail] Waiting for background Whisper to finish...")
            await transcript_event.wait()

        if session.get("transcript_error"):
            raise HTTPException(status_code=500, detail=f"Transcription failed: {session['transcript_error']}")

        video_path = session["video_path"]
        if not video_path or not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video file not found in session")

        if session.get("transcript_ready"):
            pre_transcript = session["transcript"]
    else:
        # No pre-existing session — need file or URL
        if not url and not file:
            raise HTTPException(status_code=400, detail="Must provide URL, File, or session_id")

        session_id = str(uuid.uuid4())

        if url:
            from main import download_youtube_video
            video_path, _ = download_youtube_video(url, UPLOAD_DIR)
        else:
            safe_name = os.path.basename(file.filename or "upload") or "upload"
            video_path = os.path.join(UPLOAD_DIR, f"thumb_{session_id}_{safe_name}")
            size = 0
            limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
            with open(video_path, "wb") as buffer:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit_bytes:
                        os.remove(video_path)
                        raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
                    buffer.write(chunk)

    # Meter the managed Gemini analysis (no-op for self-host).
    analyze_minutes = _cloud_config.MANAGED_ANALYSIS_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(request, analyze_minutes, session_id, "thumbnail_analyze")

    try:
        # Run analysis in thread pool (skips Whisper if pre_transcript is available)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, analyze_video_for_titles, api_key, video_path, pre_transcript)

        # Store/update session context
        if session_id not in thumbnail_sessions:
            thumbnail_sessions[session_id] = {"user_id": await _owner_id(request)}

        thumbnail_sessions[session_id].update({
            "context": result.get("transcript_summary", ""),
            "titles": result.get("titles", []),
            "language": result.get("language", "en"),
            "conversation": thumbnail_sessions[session_id].get("conversation", []),
            "video_path": video_path,
            "transcript_segments": result.get("segments", []),
            "video_duration": result.get("video_duration", 0)
        })

        if reservation_id:
            await _metering.commit_reservation(reservation_id)
        return {
            "session_id": session_id,
            "titles": result.get("titles", []),
            "context": result.get("transcript_summary", ""),
            "language": result.get("language", "en"),
            "recommended": result.get("recommended", [])
        }

    except Exception as e:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        print(f"❌ Thumbnail Analyze Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ThumbnailTitlesRequest(BaseModel):
    session_id: Optional[str] = None
    message: Optional[str] = None
    title: Optional[str] = None

@app.post("/api/thumbnail/titles")
async def thumbnail_titles(
    req: ThumbnailTitlesRequest,
    request: Request,
):
    """Refine title suggestions or accept a manual title."""
    api_key = await resolve_gemini(request)
    if not api_key:
        raise gemini_missing_error()

    # Manual title mode - just create a session with the user's title
    if req.title:
        session_id = req.session_id or str(uuid.uuid4())
        if session_id not in thumbnail_sessions:
            thumbnail_sessions[session_id] = {
                "user_id": await _owner_id(request),
                "context": "",
                "titles": [req.title],
                "language": "en",
                "conversation": []
            }
        else:
            await _assert_job_owner(request, thumbnail_sessions[session_id])
        return {"session_id": session_id, "titles": [req.title]}

    # Refinement mode
    if not req.session_id or req.session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    if not req.message:
        raise HTTPException(status_code=400, detail="Must provide message or title")

    session = thumbnail_sessions[req.session_id]
    await _assert_job_owner(request, session)

    # Add user message to conversation history
    session["conversation"].append({"role": "user", "content": req.message})

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            refine_titles,
            api_key,
            session["context"],
            req.message,
            session["conversation"]
        )

        new_titles = result.get("titles", [])
        session["titles"] = new_titles
        session["conversation"].append({"role": "assistant", "content": json.dumps(new_titles)})

        return {"titles": new_titles}

    except Exception as e:
        print(f"❌ Thumbnail Titles Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/thumbnail/generate")
async def thumbnail_generate(
    request: Request,
    session_id: str = Form(...),
    title: str = Form(...),
    extra_prompt: str = Form(""),
    count: int = Form(3),
    face: Optional[UploadFile] = File(None),
    background: Optional[UploadFile] = File(None),
):
    """Generate YouTube thumbnails with Gemini image generation."""
    api_key = await resolve_gemini(request)
    if not api_key:
        raise gemini_missing_error()

    # Image generation is the one expensive managed Gemini call — paid plans only.
    if BILLING_ENABLED:
        user = await _user_from_request(request)
        if user is not None and user.plan == "free":
            raise HTTPException(status_code=403, detail={
                "error": "plan_required",
                "message": "AI thumbnail generation is available on paid plans.",
            })

    # Clamp count
    count = min(max(1, count), 6)

    # Gemini image generation is the expensive managed call — meter it against the
    # plan quota (a batch ≈ THUMBNAIL_MINUTES). No-op for BYOK / self-host.
    thumb_minutes = _cloud_config.THUMBNAIL_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(request, thumb_minutes, session_id, "thumbnail")

    # Save optional uploaded images. basename() on the session id and filenames
    # keeps everything inside UPLOAD_DIR (no "../" escape from client input).
    face_path = None
    bg_path = None
    safe_session = os.path.basename(session_id) or "session"
    thumb_upload_dir = os.path.join(UPLOAD_DIR, f"thumb_{safe_session}")
    os.makedirs(thumb_upload_dir, exist_ok=True)

    try:
        if face and face.filename:
            face_name = os.path.basename(face.filename)
            face_path = os.path.join(thumb_upload_dir, f"face_{face_name}")
            with open(face_path, "wb") as f:
                f.write(await face.read())

        if background and background.filename:
            bg_name = os.path.basename(background.filename)
            bg_path = os.path.join(thumb_upload_dir, f"bg_{bg_name}")
            with open(bg_path, "wb") as f:
                f.write(await background.read())

        # Get video context from session (transcript summary from analysis step)
        video_context = ""
        if session_id in thumbnail_sessions:
            video_context = thumbnail_sessions[session_id].get("context", "")

        # Run generation in thread pool
        loop = asyncio.get_event_loop()
        thumbnails = await loop.run_in_executor(
            None,
            generate_thumbnail,
            api_key,
            title,
            session_id,
            face_path,
            bg_path,
            extra_prompt,
            count,
            video_context
        )

        if not thumbnails:
            raise HTTPException(status_code=500, detail="Thumbnail generation failed. Please check your Gemini API key has access to image generation (gemini-3.1-flash-image-preview model).")

        # Success — charge the reserved minutes.
        if reservation_id:
            await _metering.commit_reservation(reservation_id)
        return {"thumbnails": thumbnails}

    except HTTPException:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        raise
    except Exception as e:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        print(f"❌ Thumbnail Generate Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ThumbnailDescribeRequest(BaseModel):
    session_id: str
    title: str

@app.post("/api/thumbnail/describe")
async def thumbnail_describe(
    req: ThumbnailDescribeRequest,
    request: Request,
):
    """Generate a YouTube description with chapters from the transcript."""
    api_key = await resolve_gemini(request)
    if not api_key:
        raise gemini_missing_error()

    if req.session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = thumbnail_sessions[req.session_id]
    await _assert_job_owner(request, session)
    segments = session.get("transcript_segments", [])
    if not segments:
        raise HTTPException(status_code=400, detail="No transcript segments available. Please analyze a video first.")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            generate_youtube_description,
            api_key,
            req.title,
            segments,
            session.get("language", "en"),
            session.get("video_duration", 0)
        )
        return {"description": result.get("description", "")}

    except Exception as e:
        print(f"❌ Thumbnail Describe Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/thumbnail/publish")
async def thumbnail_publish(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    thumbnail_url: str = Form(...),
    api_key: Optional[str] = Form(None),   # BYOK; ignored for managed users
    user_id: Optional[str] = Form(None),   # BYOK profile; ignored for managed users
):
    """Kick off a background upload to YouTube via Upload-Post and return immediately."""
    if session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    # Managed users: server key + forced own profile; body fields ignored.
    upload_key, forced_profile = await resolve_upload_post(request, api_key)
    if not upload_key:
        raise HTTPException(status_code=400, detail="Missing Upload-Post API key")
    post_user = forced_profile or user_id
    if not post_user:
        raise HTTPException(status_code=400, detail="Missing Upload-Post user profile")

    session = thumbnail_sessions[session_id]
    await _assert_job_owner(request, session)
    video_path = session.get("video_path")
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Original video file not found")

    # Resolve thumbnail path from URL — sanitize against path traversal so a
    # crafted thumbnail_url (e.g. "thumbnails/../../.env") can't read server
    # files and exfiltrate them via the Upload-Post multipart body.
    thumb_relative = thumbnail_url.lstrip("/")
    if thumb_relative.startswith("thumbnails/"):
        thumb_path = _safe_under(OUTPUT_DIR, thumb_relative)
    else:
        thumb_path = _safe_under(THUMBNAILS_DIR, thumb_relative)

    if not thumb_path:
        raise HTTPException(status_code=400, detail="Invalid thumbnail path")
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail file not found")

    # Generate a unique ID for this publish job so the frontend can poll
    publish_id = str(uuid.uuid4())
    publish_jobs[publish_id] = {"status": "uploading", "result": None, "error": None}

    def do_upload():
        """Runs in a thread via BackgroundTasks — does the actual multipart upload."""
        try:
            upload_url = "https://api.upload-post.com/api/upload"
            headers = {"Authorization": f"Apikey {upload_key}"}
            data_payload = {
                "user": post_user,
                "platform[]": ["youtube"],
                "title": title,          # required base field (fallback)
                "async_upload": "true",
                "youtube_title": title,
                "youtube_description": description,
                "privacyStatus": "public",
            }
            video_filename = os.path.basename(video_path)
            thumb_filename = os.path.basename(thumb_path)

            print(f"📡 [Thumbnail] Publishing to YouTube via Upload-Post... (publish_id={publish_id})")
            with open(video_path, "rb") as vf, open(thumb_path, "rb") as tf:
                files = {
                    "video": (video_filename, vf.read(), "video/mp4"),
                    "thumbnail": (thumb_filename, tf.read(), "image/jpeg"),
                }

            # Use a long timeout — video uploads can take several minutes
            with httpx.Client(timeout=600.0) as client:
                response = client.post(upload_url, headers=headers, data=data_payload, files=files)

            if response.status_code not in [200, 201, 202]:
                err = f"Upload-Post API Error ({response.status_code}): {response.text}"
                print(f"❌ {err}")
                publish_jobs[publish_id]["status"] = "failed"
                publish_jobs[publish_id]["error"] = err
            else:
                print(f"✅ [Thumbnail] Published successfully (publish_id={publish_id})")
                publish_jobs[publish_id]["status"] = "done"
                publish_jobs[publish_id]["result"] = response.json()

        except Exception as e:
            err = str(e)
            print(f"❌ Thumbnail Publish Background Error: {err}")
            publish_jobs[publish_id]["status"] = "failed"
            publish_jobs[publish_id]["error"] = err

    background_tasks.add_task(do_upload)
    return {"publish_id": publish_id, "status": "uploading"}


@app.get("/api/thumbnail/publish/status/{publish_id}")
async def thumbnail_publish_status(publish_id: str):
    """Poll the status of a background publish job."""
    if publish_id not in publish_jobs:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return publish_jobs[publish_id]


# @app.get("/api/gallery/clips")
# async def get_gallery_clips(limit: int = 20, offset: int = 0, refresh: bool = False):
#     """
#     Fetch clips from S3 for the gallery with pagination.
#
#     Args:
#         limit: Number of clips to return (default 20, max 100)
#         offset: Starting position for pagination
#         refresh: Force refresh cache
#     """
#     try:
#         # Clamp limit to reasonable values
#         limit = min(max(1, limit), 100)
#
#         # Get clips (uses cache internally)
#         all_clips = list_all_clips(limit=limit + offset, force_refresh=refresh)
#
#         # Apply offset for pagination
#         clips = all_clips[offset:offset + limit]
#
#         return {
#             "clips": clips,
#             "total": len(all_clips),
#             "limit": limit,
#             "offset": offset,
#             "has_more": len(all_clips) > offset + limit
#         }
#     except Exception as e:
#         print(f"❌ Gallery Error: {e}")
#         raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════
# Autopilot: unattended multi-video clipping + auto-styling
# ═══════════════════════════════════════════════════════════════════════
# Additive subsystem (see autopilot.py). Fans N videos through the existing job
# queue via _create_and_enqueue_job, flags each as an autopilot child, and lets
# run_job auto-run the batch pipeline on each once its clips finalize. No
# existing endpoint changes; the single-job flow is untouched.

class AutopilotSource(BaseModel):
    # Exactly one of url / file_ref identifies the video.
    url: Optional[str] = None
    file_ref: Optional[str] = None          # path returned by /api/autopilot/upload
    label: Optional[str] = None             # display name for the board
    override: Optional[dict] = None         # sparse per-video RenderOptions override
    # Framing/length are job-creation params (CLI flags to main.py), NOT part of
    # the RenderOptions recipe — so they can't ride the recipe cascade. They get
    # their own 2-level fallback instead: None here means "inherit the batch
    # value". A mixed batch (podcast + gameplay) needs these to differ per video.
    reframe_mode: Optional[str] = None
    clip_duration_mode: Optional[str] = None
    # Per-video publish override. If set, this video publishes to these groups
    # instead of the batch-level publish plan. Shape matches the batch publish plan.
    publish: Optional[dict] = None


class AutopilotCreateRequest(BaseModel):
    recipe: dict                            # batch-level RenderOptions (model_dump shape)
    sources: List[AutopilotSource]
    output_format: str = "auto"
    reframe_mode: str = "auto"              # batch default; per-source may override
    clip_duration_mode: str = "auto"
    ack: bool = False                       # content-ownership attestation (per source)
    publish: Optional[dict] = None          # auto-publish plan (destination_ids, group_ids, platforms, clip_indexes, max_clips, schedule)


def _norm_reframe_mode(value: Optional[str], fallback: str = "auto") -> str:
    """Only 'track'/'general' steer the crop; anything else means auto-detect.
    None means 'inherit the batch value' — an explicit 'auto' does NOT inherit."""
    effective = fallback if value is None else value
    return effective if effective in ("track", "general") else "auto"


def _norm_clip_duration_mode(value: Optional[str], fallback: str = "auto") -> str:
    """Only 'short' tightens the cut; anything else means auto length.
    None means 'inherit the batch value' — an explicit 'auto' does NOT inherit."""
    effective = fallback if value is None else value
    return "short" if effective == "short" else "auto"


def _autopilot_uploads_dir() -> str:
    d = os.path.join(UPLOAD_DIR, "autopilot")
    os.makedirs(d, exist_ok=True)
    return d


@app.post("/api/autopilot/upload")
async def autopilot_upload(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    chunk: UploadFile = File(...),
):
    """Assemble-only chunked upload for autopilot sources.

    Mirrors /api/upload/chunk's chunk handling but, unlike /api/upload/complete,
    does NOT enqueue a job — it returns a server file ref once the last chunk
    lands, which the caller passes to POST /api/autopilot. Kept separate so the
    single-video upload path (lib/api.js -> complete_upload) is untouched.
    """
    if upload_id not in _chunked_uploads:
        _chunked_uploads[upload_id] = {
            "chunks": {},
            "total_chunks": total_chunks,
            "filename": chunk.filename,
            "started_at": time.time(),
            "autopilot": True,
        }
    info = _chunked_uploads[upload_id]
    chunk_path = os.path.join(UPLOAD_DIR, f"chunk_{upload_id}_{chunk_index}")
    with open(chunk_path, "wb") as f:
        f.write(await chunk.read())
    info["chunks"][chunk_index] = chunk_path

    if len(info["chunks"]) != info["total_chunks"]:
        return {"status": "received", "chunks_received": len(info["chunks"]),
                "chunks_total": total_chunks}

    # Last chunk — assemble into a stable ref (not yet a job).
    filename = info["filename"] or f"{upload_id}.mp4"
    ref_id = str(uuid.uuid4())
    final_path = os.path.join(_autopilot_uploads_dir(), f"{ref_id}_{filename}")
    try:
        with open(final_path, "wb") as out_file:
            for i in range(info["total_chunks"]):
                cp = info["chunks"][i]
                with open(cp, "rb") as cf:
                    while content := cf.read(1024 * 1024):
                        out_file.write(content)
                os.remove(cp)
        del _chunked_uploads[upload_id]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assembly failed: {e}")

    if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
        raise HTTPException(status_code=500, detail="Assembled file is empty")

    return {"status": "assembled", "file_ref": final_path,
            "filename": filename, "size": os.path.getsize(final_path)}


def _resolve_autopilot_file_ref(file_ref: str) -> str:
    """Validate a client-supplied file ref stays inside the autopilot uploads dir.

    Prevents an arbitrary server path from being passed as an input; the ref must
    be one this server handed back from /api/autopilot/upload.
    """
    base = os.path.abspath(_autopilot_uploads_dir())
    candidate = os.path.abspath(file_ref)
    if os.path.commonpath([base, candidate]) != base or not os.path.isfile(candidate):
        raise HTTPException(status_code=400, detail="Invalid or unknown file reference")
    return candidate


@app.post("/api/autopilot")
async def create_autopilot(req: AutopilotCreateRequest, request: Request):
    """Create an autopilot batch: fan sources into child jobs, each pre-loaded
    with its resolved (batch->video) recipe, flagged for auto-styling."""
    if not req.sources:
        raise HTTPException(status_code=400, detail="No sources provided")
    if not req.ack:
        raise HTTPException(status_code=400,
                            detail="You must confirm you own the content or have rights to process it.")

    # Validate the batch recipe up front (fail fast on a malformed recipe).
    try:
        batch_default = RenderOptions.model_validate(req.recipe or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid recipe: {e}")

    # The branding logo arrives as a base64 data URL under
    # branding.logo.logo_image_data (the same shape the single-video /api/process
    # path sends). LogoOverlay has no such field, so model_validate above drops
    # it — pull it from the raw payload here and write it into each child dir
    # below, mirroring _create_and_enqueue_job's branding handling.
    #
    # Two levels carry an image: the batch recipe (shared by every child) and a
    # per-video override (that one video only — e.g. a different client's logo in
    # a mixed batch). The per-video image wins; each child has its own directory,
    # so both land at the same filename without colliding.
    def _logo_data_of(payload: Optional[dict]) -> Optional[str]:
        try:
            return (((payload or {}).get("branding") or {})
                    .get("logo") or {}).get("logo_image_data")
        except Exception:
            return None

    recipe_logo_data = _logo_data_of(req.recipe)

    # Resolve keys ONCE and hold them in memory for the batch's lifetime — the
    # auto-styling runs hours later with no request in scope. Gemini flows into
    # each child job's env via _create_and_enqueue_job already; ElevenLabs has no
    # such channel, so keep it on the batch record for the translate op.
    gemini_key = await resolve_gemini(request)
    elevenlabs_key = request.headers.get("x-elevenlabs-key")

    owner = await _user_from_request(request)
    user_id = str(owner.id) if owner else None

    batch_id = str(uuid.uuid4())
    autopilot_mod.register_batch(
        batch_id, user_id, batch_default.model_dump(),
        {"gemini": gemini_key, "elevenlabs": elevenlabs_key},
        publish=req.publish,
    )

    created, errors = [], []
    for idx, src in enumerate(req.sources):
        if not src.url and not src.file_ref:
            errors.append({"index": idx, "error": "source has neither url nor file_ref"})
            continue
        input_path = None
        if src.file_ref:
            input_path = _resolve_autopilot_file_ref(src.file_ref)

        # A per-video override may carry its own logo image, which beats the
        # batch one. Strip the data URL out of the override before it travels
        # further: LogoOverlay has no such field (model_validate would drop it
        # anyway) and a base64 image has no business sitting in the in-memory
        # batch record for the hours this batch may run. src.override itself is
        # never mutated — only this rebuilt copy.
        override = src.override
        source_logo_data = _logo_data_of(override)
        if source_logo_data:
            _branding = dict(override.get("branding") or {})
            _branding["logo"] = {k: v for k, v in (_branding.get("logo") or {}).items()
                                 if k != "logo_image_data"}
            override = {**override, "branding": _branding}
        logo_data = source_logo_data or recipe_logo_data

        # Bake the batch->video cascade into this child's render_options.json so
        # the existing (default->clip) hot path serves it unchanged downstream.
        child_job_id = str(uuid.uuid4())
        child_dir = os.path.join(OUTPUT_DIR, child_job_id)
        os.makedirs(child_dir, exist_ok=True)
        try:
            resolved = resolve_cascade(batch_default, override)
            # If a logo applies to this child and branding is enabled for it
            # (post-cascade), decode it into the child dir so the branding
            # processor — which self-loads render_options.json and reads
            # logo.image_path from disk — actually finds it.
            branding_on = bool(resolved.branding and resolved.branding.logo.enabled)
            if (logo_data and isinstance(logo_data, str)
                    and logo_data.startswith("data:image") and branding_on):
                try:
                    import base64 as _b64
                    _, encoded = logo_data.split(",", 1)
                    with open(os.path.join(child_dir, "branding_logo.png"), "wb") as lf:
                        lf.write(_b64.b64decode(encoded))
                    resolved.branding.logo.image_path = "branding_logo.png"
                except Exception as e:
                    print(f"⚠️ Autopilot branding logo write failed for {child_job_id}: {e}")
            if branding_on and not resolved.branding.logo.image_path:
                # Branding switched on with no image at either level. apply_branding
                # bails on a missing image_path, so the op would burn a pass and
                # silently emit an unbranded clip. Turn it off instead of lying.
                print(f"⚠️ Autopilot: branding enabled for {child_job_id} with no logo "
                      f"image at batch or video level; skipping branding for it.")
                resolved.branding.logo.enabled = False
            save_options(child_dir, resolved, {})
        except Exception as e:
            errors.append({"index": idx, "error": f"recipe resolve failed: {e}"})
            continue

        try:
            result = await _create_and_enqueue_job(
                request,
                input_path=input_path,
                url=src.url,
                ack_flag=True,
                output_format=req.output_format,
                branding_payload=None,           # recipe already saved above
                force_low=True,                  # skip the interactive URL quality gate
                job_id=child_job_id,
                reframe_mode=_norm_reframe_mode(src.reframe_mode, req.reframe_mode),
                clip_duration_mode=_norm_clip_duration_mode(
                    src.clip_duration_mode, req.clip_duration_mode),
                autopilot_batch_id=batch_id,
            )
        except HTTPException as he:
            errors.append({"index": idx, "error": he.detail})
            continue
        except Exception as e:
            errors.append({"index": idx, "error": str(e)})
            continue

        label = src.label or (src.url or os.path.basename(input_path or f"video {idx+1}"))
        kind = "url" if src.url else "file"
        # `override`, not src.override — the logo data URL is already on disk.
        autopilot_mod.attach_child(batch_id, child_job_id, label, kind, override)
        # Store per-video publish override if provided
        if src.publish:
            autopilot_mod.set_publish_override(batch_id, child_job_id, src.publish)
        created.append(result.get("job_id", child_job_id))

    if not created:
        # Nothing enqueued — surface the batch as failed rather than an empty run.
        autopilot_mod.cancel_batch(batch_id)
        raise HTTPException(status_code=400,
                            detail={"message": "No jobs could be created", "errors": errors})

    return {"batch_id": batch_id, "created": created, "errors": errors,
            "total": len(created)}


@app.get("/api/autopilot")
async def list_autopilot(request: Request):
    """List the caller's autopilot batches (board rehydrate on reload)."""
    owner = await _user_from_request(request)
    user_id = str(owner.id) if owner else None
    return {"batches": autopilot_mod.list_batches(user_id, jobs)}


@app.get("/api/autopilot/{batch_id}")
async def get_autopilot(batch_id: str, request: Request):
    """Aggregate per-video progress across a batch's child jobs."""
    rec = autopilot_mod.autopilot_batches.get(batch_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Autopilot batch not found")
    await _assert_job_owner(request, {"user_id": rec.get("user_id")})
    prog = autopilot_mod.progress(batch_id, jobs)
    if prog is None:
        raise HTTPException(status_code=404, detail="Autopilot batch not found")
    return prog


@app.post("/api/autopilot/{batch_id}/cancel")
async def cancel_autopilot(batch_id: str, request: Request):
    """Cancel a batch: flag it (blocks not-yet-fired auto-styling) and cancel any
    in-flight per-clip batches on its children. Queued child jobs still run to
    completion but skip auto-styling once they finish."""
    rec = autopilot_mod.autopilot_batches.get(batch_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Autopilot batch not found")
    await _assert_job_owner(request, {"user_id": rec.get("user_id")})

    autopilot_mod.cancel_batch(batch_id)
    cancelled_batches = 0
    for child_id in list(rec.get("child_job_ids", [])):
        batch = (jobs.get(child_id) or {}).get("batch")
        if batch is not None and getattr(batch, "status", None) == "running":
            try:
                batch.cancel()
                cancelled_batches += 1
            except Exception:
                pass
    return {"success": True, "cancelled_active_batches": cancelled_batches}


class AutopilotManualPublishRequest(BaseModel):
    """Manually trigger auto-publish for a completed job within an autopilot batch."""
    job_id: str
    group_ids: List[str] = []
    platforms: List[str] = []
    max_clips: Optional[int] = None
    schedule: Optional[str] = None  # 'immediate' | 'spread'


@app.post("/api/autopilot/{batch_id}/publish")
async def autopilot_manual_publish(
    batch_id: str,
    req: AutopilotManualPublishRequest,
    request: Request,
):
    """Manually trigger auto-publish for a completed job within an autopilot batch.

    Used when a user didn't enable auto-publish at setup time, but wants to publish
    after reviewing the generated clips. Requires the job to be in 'done' stage.
    """
    if not PUBLISHING_ENABLED:
        raise HTTPException(status_code=400, detail="Publishing is not enabled on this server")

    rec = autopilot_mod.autopilot_batches.get(batch_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Autopilot batch not found")

    await _assert_job_owner(request, {"user_id": rec.get("user_id")})

    # Verify the job belongs to this batch
    if req.job_id not in rec.get("child_job_ids", []):
        raise HTTPException(status_code=400, detail="Job does not belong to this autopilot batch")

    job = jobs.get(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check job is done and has clips
    result = job.get("result") or {}
    clips = result.get("clips") or []
    if not clips:
        raise HTTPException(status_code=400, detail="Job has no clips to publish")

    # Build publish plan from request
    plan = {
        "destination_ids": [],
        "group_ids": req.group_ids,
        "platforms": req.platforms,
        "clip_indexes": None,
        "max_clips": req.max_clips,
        "schedule": req.schedule or "immediate",
    }

    # Validate at least one destination is specified
    if not plan["group_ids"] and not plan["destination_ids"]:
        raise HTTPException(status_code=400, detail="Specify at least one group or destination")

    user_id = job.get("user_id")

    async def _do_publish():
        try:
            from publishing import autopublish
            report = await autopublish.publish_job_clips(
                req.job_id, len(clips), plan, user_id=user_id, actor="manual"
            )
            n = len(report.get('created') or [])
            skipped = len(report.get('skipped') or [])
            job.setdefault('logs', []).append(
                f"Manual publish: {n} post(s) created"
                + (f", {skipped} clip(s) skipped." if skipped else "."))
            return report
        except Exception as e:
            print(f"⚠️  Manual publish failed for {req.job_id}: {e}")
            job.setdefault('logs', []).append(f"Manual publish failed: {e}")
            raise

    # Run async and return immediately
    loop = asyncio.get_event_loop()
    asyncio.run_coroutine_threadsafe(_do_publish(), loop)

    return {
        "success": True,
        "message": f"Publishing job {req.job_id} scheduled",
        "clip_count": len(clips),
    }


# ═══════════════════════════════════════════════════════════════════════
# SaaSShorts: AI UGC Video Generator for SaaS Products
# ═══════════════════════════════════════════════════════════════════════

from saasshorts import (
    scrape_website,
    research_saas_online,
    analyze_saas,
    generate_scripts,
    generate_full_video,
    generate_actor_images,
    get_elevenlabs_voices,
    DEFAULT_VOICES,
)

# State for SaaSShorts jobs (separate from video processing jobs)
saas_jobs: Dict[str, Dict] = {}


class SaaSAnalyzeRequest(BaseModel):
    url: Optional[str] = None
    description: Optional[str] = None  # Manual product/business description
    num_scripts: int = 3
    style: str = "ugc"
    language: str = "en"
    actor_gender: str = "female"


@app.post("/api/saasshorts/analyze")
async def saasshorts_analyze(
    req: SaaSAnalyzeRequest,
    request: Request,
):
    """Analyze a URL or manual description and generate video scripts."""
    gemini_key = await resolve_gemini(request)
    if not gemini_key:
        raise gemini_missing_error()

    if not req.url and not req.description:
        raise HTTPException(status_code=400, detail="Provide a URL or a product description")

    # Meter the managed Gemini research/analysis (no-op for self-host).
    saas_minutes = _cloud_config.MANAGED_ANALYSIS_MINUTES if BILLING_ENABLED else 0
    reservation_id = await reserve_managed_action(request, saas_minutes, "saasshorts", "saasshorts_analyze")

    try:
        loop = asyncio.get_event_loop()

        def run_analysis():
            web_research = None

            if req.url and req.url.strip():
                # URL provided: full scrape + research pipeline
                scraped = scrape_website(req.url)
                web_research = research_saas_online(req.url, gemini_key)
                analysis = analyze_saas(scraped, gemini_key, web_research=web_research)
            else:
                # Manual description: build analysis from description
                analysis = {
                    "product_name": req.description.split(",")[0].strip()[:60] if req.description else "Product",
                    "description": req.description,
                    "value_proposition": req.description,
                    "target_audience": "general audience",
                    "key_features": [req.description],
                    "pain_points": [],
                    "tone": "casual and authentic",
                }

            scripts = generate_scripts(analysis, gemini_key, req.num_scripts, req.style, req.language, req.actor_gender)
            return {
                "analysis": analysis,
                "scripts": scripts,
                "web_research": web_research,
            }

        result = await loop.run_in_executor(None, run_analysis)
        if reservation_id:
            await _metering.commit_reservation(reservation_id)
        return result

    except Exception as e:
        if reservation_id:
            await _metering.release_reservation(reservation_id)
        raise HTTPException(status_code=500, detail=str(e))


class SaaSActorRequest(BaseModel):
    actor_description: str
    num_options: int = 3
    product_description: Optional[str] = None


@app.post("/api/saasshorts/actor-upload")
async def saasshorts_actor_upload(request: Request, file: UploadFile = File(...)):
    """Upload a custom actor image (stored locally only, not S3)."""
    # SaaSShorts is part of the paid product — require entitlement in cloud mode
    # (no-op for self-host) so anonymous callers can't drive server work.
    await require_managed_entitlement(request)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        # Bounded read: an actor image has no business being large. Cap it so an
        # anonymous caller can't stream a multi-GB body into RAM (OOM DoS).
        ACTOR_IMAGE_MAX_BYTES = 25 * 1024 * 1024  # 25 MB
        content = await file.read(ACTOR_IMAGE_MAX_BYTES + 1)
        if len(content) > ACTOR_IMAGE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Image too large (max 25 MB)")

        # Validate minimum size
        if len(content) < 1000:
            raise HTTPException(status_code=400, detail="File too small to be a valid image")

        upload_id = uuid.uuid4().hex[:8]
        upload_dir = os.path.join(OUTPUT_DIR, "actor_uploads")
        os.makedirs(upload_dir, exist_ok=True)
        filename = f"custom_{upload_id}.png"
        file_path = os.path.join(upload_dir, filename)

        with open(file_path, "wb") as f:
            f.write(content)

        return {"url": f"/videos/actor_uploads/{filename}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/saasshorts/actor-options")
async def saasshorts_actor_options(
    req: SaaSActorRequest,
    request: Request,
    x_fal_key: Optional[str] = Header(None, alias="X-Fal-Key"),
):
    """Generate multiple actor image options for the user to choose from."""
    await require_managed_entitlement(request)
    fal_key = x_fal_key
    if not fal_key:
        raise HTTPException(status_code=400, detail="Missing fal.ai API Key")

    try:
        job_id = str(uuid.uuid4())
        out_dir = os.path.join(OUTPUT_DIR, f"saas_actors_{job_id}")
        os.makedirs(out_dir, exist_ok=True)

        loop = asyncio.get_running_loop()
        import functools
        paths = await loop.run_in_executor(
            None,
            functools.partial(
                generate_actor_images,
                req.actor_description, fal_key, out_dir, "actor", req.num_options,
                product_description=req.product_description,
            ),
        )

        # Upload each actor image to public S3 with description
        desc = req.actor_description
        if req.product_description:
            desc += f" (holding {req.product_description})"
        urls = []
        for p in paths:
            s3_url = upload_actor_to_s3(p, description=desc)
            if s3_url:
                urls.append(s3_url)
            else:
                # Fallback to local URL if S3 fails
                urls.append(f"/videos/saas_actors_{job_id}/{os.path.basename(p)}")

        return {"images": urls}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/saasshorts/gallery")
async def saasshorts_video_gallery(limit: int = 50):
    """List all UGC videos from the public gallery."""
    try:
        loop = asyncio.get_running_loop()
        videos = await loop.run_in_executor(None, list_video_gallery, limit)
        return {"videos": videos, "total": len(videos)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SaaSPostRequest(BaseModel):
    job_id: str
    api_key: Optional[str] = None  # BYOK; ignored for managed users
    user_id: Optional[str] = None  # BYOK profile; ignored for managed users
    platforms: List[str]
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_date: Optional[str] = None
    timezone: Optional[str] = "UTC"


@app.post("/api/saasshorts/post")
async def saasshorts_post_to_socials(req: SaaSPostRequest, request: Request):
    """Post an AI Shorts video to social media via Upload-Post."""
    if req.job_id not in saas_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    upload_key, forced_profile = await resolve_upload_post(request, req.api_key)
    if not upload_key:
        raise HTTPException(status_code=400, detail="Missing Upload-Post API key")
    post_user = forced_profile or req.user_id
    if not post_user:
        raise HTTPException(status_code=400, detail="Missing Upload-Post user profile")

    job = saas_jobs[req.job_id]
    await _assert_job_owner(request, job)
    result = job.get("result")
    if not result or not result.get("video_url"):
        raise HTTPException(status_code=400, detail="No video available for this job")

    try:
        # Resolve video file path
        video_url = result["video_url"]  # e.g. /videos/saas_xxx/slug_final.mp4
        rel_path = video_url.replace("/videos/", "")
        file_path = os.path.join(OUTPUT_DIR, rel_path)

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"Video file not found")

        script = result.get("script", {})
        final_title = req.title or script.get("title", "AI Short")
        final_description = req.description or script.get("caption", "")
        if not final_description:
            final_description = script.get("full_narration", "Check this out!")

        url = "https://api.upload-post.com/api/upload"
        headers = {"Authorization": f"Apikey {upload_key}"}

        data_payload = {
            "user": post_user,
            "title": final_title,
            "platform[]": req.platforms,
            "async_upload": "true",
            # Every platform posts PUBLIC. Without this, TikTok falls back to
            # its "moi uniquement" (private) default and never surfaces as a
            # confirmed public post.
            "privacyStatus": "public",
        }

        if req.scheduled_date:
            data_payload["scheduled_date"] = req.scheduled_date
            if req.timezone:
                data_payload["timezone"] = req.timezone

        if "tiktok" in req.platforms:
            data_payload["tiktok_title"] = final_description
        if "instagram" in req.platforms:
            data_payload["instagram_title"] = final_description
            data_payload["media_type"] = "REELS"
        if "youtube" in req.platforms:
            data_payload["youtube_title"] = final_title
            data_payload["youtube_description"] = final_description
            data_payload["privacyStatus"] = "public"

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            file_content = f.read()

        files = {"video": (filename, file_content, "video/mp4")}

        with httpx.Client(timeout=120.0) as client:
            print(f"📡 [AI Shorts] Sending to Upload-Post: {req.platforms}")
            response = client.post(url, headers=headers, data=data_payload, files=files)

        if response.status_code not in [200, 201, 202]:
            raise HTTPException(status_code=response.status_code, detail=f"Upload-Post Error: {response.text}")

        return response.json()

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [AI Shorts] Post Exception: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/gallery", response_class=HTMLResponse)
async def gallery_html_page():
    """SEO gallery page with all generated UGC videos."""
    import html as html_mod
    loop = asyncio.get_running_loop()
    videos = await loop.run_in_executor(None, list_video_gallery, 100)

    cards_html = ""
    ld_items = []
    for i, v in enumerate(videos):
        title = html_mod.escape(v.get("title", "Untitled"))
        video_url = v.get("video_url", "")
        actor_url = v.get("actor_url", "")
        video_id = v.get("video_id", "")
        duration = v.get("duration", 0)
        mode = v.get("video_mode", "")
        product = html_mod.escape(v.get("product_name", ""))
        caption = html_mod.escape(v.get("caption", "")[:120])

        mode_badge = '<span style="background:#22c55e;color:#000;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:700">LOW COST</span>' if mode == "lowcost" else '<span style="background:#8b5cf6;color:#fff;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:700">PREMIUM</span>'

        cards_html += f'''
        <a href="/video/{video_id}" style="text-decoration:none;color:inherit">
          <div style="background:#18181b;border-radius:16px;overflow:hidden;border:1px solid #27272a;transition:transform 0.2s" onmouseover="this.style.transform='scale(1.02)'" onmouseout="this.style.transform='scale(1)'">
            <div style="position:relative;aspect-ratio:9/16;background:#000">
              <video src="{video_url}" poster="{actor_url}" muted playsinline preload="metadata"
                     onmouseenter="this.play()" onmouseleave="this.pause();this.currentTime=0"
                     style="width:100%;height:100%;object-fit:cover"></video>
              <div style="position:absolute;top:8px;right:8px">{mode_badge}</div>
            </div>
            <div style="padding:12px">
              <h2 style="font-size:14px;font-weight:600;margin:0 0 4px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{title}</h2>
              <p style="font-size:11px;color:#71717a;margin:0">{duration:.0f}s · {product}</p>
            </div>
          </div>
        </a>'''

        ld_items.append(f'{{"@type":"ListItem","position":{i+1},"url":"https://openshorts.app/video/{video_id}","name":"{title}"}}')

    ld_json = f'{{"@context":"https://schema.org","@type":"CollectionPage","name":"AI UGC Video Gallery","mainEntity":{{"@type":"ItemList","numberOfItems":{len(videos)},"itemListElement":[{",".join(ld_items)}]}}}}'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI UGC Video Gallery | OpenShorts</title>
<meta name="description" content="Browse {len(videos)} AI-generated UGC marketing videos. Create viral TikTok and Instagram Reels for your SaaS product.">
<meta name="robots" content="index, follow">
<meta property="og:title" content="AI UGC Video Gallery | OpenShorts">
<meta property="og:type" content="website">
<meta property="og:description" content="Browse AI-generated UGC marketing videos for SaaS products.">
<script type="application/ld+json">{ld_json}</script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0c;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,sans-serif}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:20px;padding:20px;max-width:1400px;margin:0 auto}}
nav{{padding:20px 40px;border-bottom:1px solid #27272a;display:flex;align-items:center;justify-content:space-between}}
h1{{font-size:28px;font-weight:700;padding:40px 20px 0;text-align:center}}
.subtitle{{text-align:center;color:#71717a;font-size:14px;padding:8px 20px 20px}}
.cta{{display:inline-block;background:#8b5cf6;color:#fff;padding:10px 24px;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px}}
</style>
</head>
<body>
<nav><strong style="font-size:18px">OpenShorts</strong><a href="/" class="cta">Create Your Video</a></nav>
<h1>AI-Generated UGC Videos</h1>
<p class="subtitle">{len(videos)} videos generated · Low Cost & Premium modes</p>
<div class="grid">{cards_html}</div>
<div style="text-align:center;padding:40px"><a href="/" class="cta">Create Your Own UGC Video</a></div>
</body></html>'''


@app.get("/video/{video_id}", response_class=HTMLResponse)
async def video_html_page(video_id: str):
    """SEO individual video page with og:video meta tags."""
    import html as html_mod
    loop = asyncio.get_running_loop()
    videos = await loop.run_in_executor(None, list_video_gallery, 200)
    meta = next((v for v in videos if v.get("video_id") == video_id), None)
    if not meta:
        raise HTTPException(status_code=404, detail="Video not found")

    title = html_mod.escape(meta.get("title", "Untitled"))
    caption = html_mod.escape(meta.get("caption", ""))
    narration = html_mod.escape(meta.get("full_narration", ""))
    video_url = meta.get("video_url", "")
    actor_url = meta.get("actor_url", "")
    duration = meta.get("duration", 0)
    mode = meta.get("video_mode", "")
    product = html_mod.escape(meta.get("product_name", ""))
    product_url = html_mod.escape(meta.get("product_url", ""))
    language = meta.get("language", "en")
    hashtags = " ".join(meta.get("hashtags", []))
    cost = meta.get("cost_estimate", {}).get("total", 0)
    created = meta.get("created_at", "")
    actor_desc = html_mod.escape(meta.get("actor_description", ""))

    ld_json = f'{{"@context":"https://schema.org","@type":"VideoObject","name":"{title}","description":"{caption}","thumbnailUrl":"{actor_url}","contentUrl":"{video_url}","uploadDate":"{created}","duration":"PT{int(duration)}S","width":1080,"height":1920,"inLanguage":"{language}"}}'

    mode_label = "Low Cost" if mode == "lowcost" else "Premium"

    return f'''<!DOCTYPE html>
<html lang="{language}">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - AI UGC Video | OpenShorts</title>
<meta name="description" content="{caption} {hashtags}">
<meta property="og:type" content="video.other">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{caption}">
<meta property="og:video" content="{video_url}">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="1080">
<meta property="og:video:height" content="1920">
<meta property="og:image" content="{actor_url}">
<meta name="twitter:card" content="player">
<meta name="twitter:title" content="{title}">
<meta name="twitter:image" content="{actor_url}">
<script type="application/ld+json">{ld_json}</script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0c;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,sans-serif}}
nav{{padding:20px 40px;border-bottom:1px solid #27272a;display:flex;align-items:center;gap:16px}}
nav a{{color:#a1a1aa;text-decoration:none;font-size:14px}}
.container{{max-width:1000px;margin:0 auto;padding:40px 20px;display:grid;grid-template-columns:1fr 1fr;gap:40px}}
@media(max-width:768px){{.container{{grid-template-columns:1fr}}}}
video{{width:100%;border-radius:16px;background:#000}}
h1{{font-size:22px;font-weight:700;margin-bottom:8px}}
.meta{{color:#71717a;font-size:13px;margin-bottom:20px}}
.section{{margin-bottom:20px}}
.section h2{{font-size:13px;color:#71717a;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
.section p{{font-size:14px;line-height:1.6}}
.badge{{display:inline-block;padding:3px 10px;border-radius:9999px;font-size:11px;font-weight:700}}
.cta{{display:inline-block;background:#8b5cf6;color:#fff;padding:10px 24px;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px;margin-top:20px}}
</style>
</head>
<body>
<nav><strong>OpenShorts</strong><a href="/gallery">Gallery</a><span style="color:#3f3f46">›</span><span style="color:#e4e4e7;font-size:14px">{title}</span></nav>
<div class="container">
<div><video src="{video_url}" poster="{actor_url}" controls autoplay playsinline style="aspect-ratio:9/16;object-fit:cover"></video></div>
<div>
<h1>{title}</h1>
<p class="meta">{duration:.0f}s · {mode_label} · ${cost:.2f} · {product}</p>
<div class="section"><h2>Caption</h2><p>{caption}</p><p style="color:#8b5cf6;margin-top:4px">{hashtags}</p></div>
<div class="section"><h2>Script</h2><p>{narration}</p></div>
<div class="section"><h2>Actor</h2><p>{actor_desc}</p></div>
{f'<div class="section"><h2>Product</h2><p><a href="{product_url}" style="color:#8b5cf6" target="_blank">{product}</a></p></div>' if product_url else ''}
<a href="/gallery">← Back to Gallery</a>
<br><a href="/" class="cta">Create Your Own</a>
</div>
</div>
</body></html>'''


@app.get("/api/saasshorts/actor-gallery")
async def saasshorts_actor_gallery():
    """List all previously generated actor images from public S3."""
    try:
        loop = asyncio.get_running_loop()
        images = await loop.run_in_executor(None, list_actor_gallery)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SaaSGenerateRequest(BaseModel):
    script: dict
    voice_id: Optional[str] = None
    actor_description: Optional[str] = None
    selected_actor_url: Optional[str] = None  # Pre-selected actor image URL
    retry_job_id: Optional[str] = None
    video_mode: str = "lowcost"  # "lowcost" or "premium"
    # Publishing to the public /gallery is opt-in: generated videos carry the
    # user's product name, URL and full script.
    share_to_gallery: bool = False


@app.post("/api/saasshorts/generate")
async def saasshorts_generate(
    req: SaaSGenerateRequest,
    request: Request,
    x_fal_key: Optional[str] = Header(None, alias="X-Fal-Key"),
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key"),
):
    """Generate a SaaS UGC video from a script. Returns a job_id for polling."""
    await require_managed_entitlement(request)
    fal_key = x_fal_key
    elevenlabs_key = x_elevenlabs_key

    if not fal_key:
        raise HTTPException(status_code=400, detail="Missing fal.ai API Key (X-Fal-Key header)")
    if not elevenlabs_key:
        raise HTTPException(status_code=400, detail="Missing ElevenLabs API Key (X-ElevenLabs-Key header)")

    # Support retry: reuse output_dir so cached assets (image, voice, head, broll) are kept
    reused = False
    if req.retry_job_id:
        # Check memory first, then disk. _safe_under() blocks a crafted
        # retry_job_id like "../../tmp/x" from escaping OUTPUT_DIR (the listdir
        # below deletes files and the pipeline writes here). A known in-memory
        # job keeps its trusted stored path.
        if req.retry_job_id in saas_jobs:
            await _assert_job_owner(request, saas_jobs[req.retry_job_id])
            old_dir = saas_jobs[req.retry_job_id]["output_dir"]
        else:
            old_dir = _safe_under(OUTPUT_DIR, f"saas_{req.retry_job_id}")

        if old_dir and os.path.isdir(old_dir):
            job_id = req.retry_job_id
            job_output_dir = old_dir
            reused = True
            # Clear the 0-byte final video so pipeline re-generates it
            for f in os.listdir(old_dir):
                fp = os.path.join(old_dir, f)
                if f.endswith("_final.mp4") and os.path.getsize(fp) == 0:
                    os.remove(fp)
            saas_jobs[job_id] = {
                "user_id": await _owner_id(request),
                "status": "processing",
                "logs": [f"Retrying job {job_id[:8]}... reusing cached assets from disk."],
                "result": None,
                "output_dir": job_output_dir,
            }

    if not reused:
        job_id = str(uuid.uuid4())
        job_output_dir = os.path.join(OUTPUT_DIR, f"saas_{job_id}")
        os.makedirs(job_output_dir, exist_ok=True)
        saas_jobs[job_id] = {
            "user_id": await _owner_id(request),
            "status": "processing",
            "logs": ["SaaSShorts job started."],
            "result": None,
            "output_dir": job_output_dir,
        }

    # If user selected a pre-generated actor, resolve it to a local path
    selected_actor_path = None
    if req.selected_actor_url:
        if req.selected_actor_url.startswith("http"):
            # Download from S3 public URL to job output dir
            import httpx
            from security_utils import assert_public_url
            try:
                # SSRF guard: block private / metadata hosts before fetching.
                safe_actor_url = assert_public_url(req.selected_actor_url)
                actor_local = os.path.join(job_output_dir, "selected_actor.png")
                with httpx.Client(timeout=30.0) as client:
                    resp = client.get(safe_actor_url)
                    if resp.status_code == 200:
                        with open(actor_local, "wb") as f:
                            f.write(resp.content)
                        selected_actor_path = actor_local
            except Exception:
                pass
        else:
            # Sanitize against traversal — the client controls selected_actor_url.
            src = _safe_under(OUTPUT_DIR, req.selected_actor_url.replace("/videos/", "").lstrip("/"))
            if src and os.path.exists(src):
                selected_actor_path = src

    config = {
        "fal_key": fal_key,
        "elevenlabs_key": elevenlabs_key,
        "voice_id": req.voice_id or "21m00Tcm4TlvDq8ikWAM",
        "actor_description": req.actor_description,
        "selected_actor_path": selected_actor_path,
        "video_mode": req.video_mode,
    }

    async def run_generation():
        await concurrency_semaphore.acquire()
        try:
            loop = asyncio.get_running_loop()

            def log_msg(msg):
                print(f"[SaaSShorts Job {job_id[:8]}] {msg}")
                if job_id in saas_jobs:
                    saas_jobs[job_id]["logs"].append(msg)

            def run():
                return generate_full_video(req.script, config, job_output_dir, log_msg)

            result = await loop.run_in_executor(None, run)

            if job_id in saas_jobs:
                video_filename = result["video_filename"]
                saas_jobs[job_id]["status"] = "completed"
                saas_jobs[job_id]["result"] = {
                    "video_url": f"/videos/saas_{job_id}/{video_filename}",
                    "video_filename": video_filename,
                    "duration": result.get("duration", 0),
                    "cost_estimate": result.get("cost_estimate", {}),
                    "script": req.script,
                }
                saas_jobs[job_id]["logs"].append("Video generation completed!")

                # Upload to public gallery — opt-in only: the metadata carries
                # the user's product name, URL and full script.
                if req.share_to_gallery:
                    try:
                        gallery_meta = {
                            "title": req.script.get("title", "Untitled"),
                            "hook_text": req.script.get("hook_text", ""),
                            "caption": req.script.get("caption", ""),
                            "hashtags": req.script.get("hashtags", []),
                            "full_narration": req.script.get("full_narration", ""),
                            "actor_description": req.script.get("actor_description", ""),
                            "style": req.script.get("style", "ugc"),
                            "language": req.script.get("language", "en"),
                            "duration": result.get("duration", 0),
                            "video_mode": req.video_mode,
                            "product_name": req.script.get("_product_name", ""),
                            "product_url": req.script.get("_product_url", ""),
                            "segments": req.script.get("segments", []),
                            "cost_estimate": result.get("cost_estimate", {}),
                        }
                        gallery_result = upload_video_to_gallery(
                            video_path=result["video_path"],
                            actor_image_path=result.get("actor_image", ""),
                            metadata=gallery_meta,
                            video_id=job_id[:8],
                        )
                        if gallery_result:
                            saas_jobs[job_id]["result"]["gallery_video_id"] = gallery_result["video_id"]
                            log_msg("📤 Uploaded to public gallery.")
                    except Exception as gallery_err:
                        log_msg(f"⚠️ Gallery upload skipped: {gallery_err}")

        except Exception as e:
            print(f"[SaaSShorts] ❌ Job {job_id} failed: {e}")
            if job_id in saas_jobs:
                saas_jobs[job_id]["status"] = "failed"
                saas_jobs[job_id]["logs"].append(f"Error: {str(e)}")
        finally:
            concurrency_semaphore.release()

    asyncio.create_task(run_generation())

    return {"job_id": job_id, "status": "processing"}


@app.get("/api/saasshorts/status/{job_id}")
async def saasshorts_status(job_id: str, request: Request):
    """Poll SaaSShorts job status."""
    if job_id not in saas_jobs:
        raise HTTPException(status_code=404, detail="SaaSShorts job not found")

    job = saas_jobs[job_id]
    await _assert_job_owner(request, job)
    return {
        "status": job["status"],
        "logs": job["logs"],
        "result": job.get("result"),
    }


@app.get("/api/saasshorts/voices")
async def saasshorts_voices(
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key"),
):
    """List available ElevenLabs voices."""
    if x_elevenlabs_key:
        try:
            loop = asyncio.get_event_loop()
            voices = await loop.run_in_executor(
                None, get_elevenlabs_voices, x_elevenlabs_key
            )
            if voices:
                return {"voices": voices, "source": "elevenlabs"}
        except Exception:
            pass

    # Fallback to default voices
    return {
        "voices": [
            {"voice_id": vid, "name": name, "category": "default"}
            for name, vid in DEFAULT_VOICES.items()
        ],
        "source": "defaults",
    }
