"""Generic batch processing pipeline for post-clip operations.

The orchestrator is operation-agnostic: it knows only that it has a list of
operations to execute per clip, dispatched through a registry. Adding a new
operation requires only registering it in OPERATIONS — no orchestrator changes.

Each registered operation provides:
  - processor: callable(job_id, clip_index, config, output_dir) -> new_filename
  - validator: Pydantic model class for config validation
  - resource: concurrency pool tag ("ffmpeg", "gemini", "elevenlabs")
  - label: human-readable progress label
"""

import os
import re
import json
import glob
import threading
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BatchCancelled(Exception):
    pass


class ProcessorError(Exception):
    def __init__(self, message: str, step: str):
        super().__init__(message)
        self.step = step


# ---------------------------------------------------------------------------
# Shared helpers (extracted from endpoint inline logic)
# ---------------------------------------------------------------------------

def resolve_clip_filename(job_id: str, clip_index: int, output_dir: str,
                          input_filename: Optional[str] = None) -> str:
    """Resolve the current video filename for a clip, walking back subtitle prefixes."""
    if input_filename:
        return os.path.basename(input_filename)

    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise ProcessorError("Metadata not found", "resolve")

    with open(json_files[0], "r") as f:
        data = json.load(f)

    clips = data.get("shorts", [])
    if clip_index >= len(clips):
        raise ProcessorError(f"Clip {clip_index} not found in metadata", "resolve")

    clip_data = clips[clip_index]
    filename = clip_data.get("video_url", "").split("/")[-1]
    if not filename:
        base_name = os.path.basename(json_files[0]).replace("_metadata.json", "")
        filename = f"{base_name}_clip_{clip_index + 1}.mp4"
    return filename


def persist_video_url(job_id: str, clip_index: int, new_filename: str,
                      output_dir: str, jobs: dict):
    """Update video_url in both the in-memory job and the on-disk metadata."""
    new_url = f"/videos/{job_id}/{new_filename}"

    job = jobs.get(job_id)
    if job and "result" in job and "clips" in job["result"]:
        clips = job["result"]["clips"]
        if clip_index < len(clips):
            clips[clip_index]["video_url"] = new_url

    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if json_files:
        try:
            with open(json_files[0], "r") as f:
                data = json.load(f)
            shorts = data.get("shorts", [])
            if clip_index < len(shorts):
                shorts[clip_index]["video_url"] = new_url
                data["shorts"] = shorts
                with open(json_files[0], "w") as f:
                    json.dump(data, f, indent=4)
        except Exception as e:
            print(f"⚠️ batch: failed to persist metadata for clip {clip_index}: {e}")


def get_clip_metadata(job_id: str, clip_index: int, output_dir: str) -> dict:
    """Load clip metadata (start, end, transcript, etc.) from the metadata JSON."""
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise ProcessorError("Metadata not found", "resolve")
    with open(json_files[0], "r") as f:
        data = json.load(f)
    clips = data.get("shorts", [])
    if clip_index >= len(clips):
        raise ProcessorError(f"Clip {clip_index} not found", "resolve")
    return {
        "clip_data": clips[clip_index],
        "transcript": data.get("transcript"),
        "all_clips": clips,
        "metadata_path": json_files[0],
        "metadata": data,
    }


# ---------------------------------------------------------------------------
# Processor functions (extracted from app.py endpoint handlers)
# ---------------------------------------------------------------------------

def _apply_subtitle(job_id: str, clip_index: int, config: dict,
                    output_dir: str) -> str:
    """Burn subtitles onto a clip. Returns new filename."""
    from subtitles import generate_srt, generate_ass, burn_subtitles, generate_srt_from_video

    meta = get_clip_metadata(job_id, clip_index, output_dir)
    clip_data = meta["clip_data"]
    transcript = meta["transcript"]

    filename = resolve_clip_filename(job_id, clip_index, output_dir,
                                    config.get("input_filename"))

    # Walk back previous subtitle burns
    while True:
        m = re.match(r"^subtitled_\d+_(.+)$", filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            break
        filename = m.group(1)

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise ProcessorError(f"Video file not found: {filename}", "subtitle")

    style = config.get("style", "classic")
    is_karaoke = style == "karaoke"
    generation_id = int(time.time())
    srt_filename = f"subs_{clip_index}_{generation_id}.{'ass' if is_karaoke else 'srt'}"
    srt_path = os.path.join(output_dir, srt_filename)

    position = config.get("position", "bottom")
    font_size = config.get("font_size", 16)
    font_name = config.get("font_name", "Verdana")
    font_color = config.get("font_color", "#FFFFFF")
    border_color = config.get("border_color", "#000000")
    border_width = config.get("border_width", 2)
    bg_color = config.get("bg_color", "#000000")
    bg_opacity = config.get("bg_opacity", 0.0)
    highlight_color = config.get("highlight_color", "#FFD700")
    effect = config.get("effect", "none")
    base_opacity = config.get("base_opacity", 1.0)
    uppercase = config.get("uppercase", False)

    karaoke_opts = dict(
        alignment=position, fontsize=font_size, font_name=font_name,
        font_color=font_color, border_color=border_color,
        border_width=border_width, highlight_color=highlight_color,
        bg_color=bg_color, bg_opacity=bg_opacity,
        effect=effect, base_opacity=base_opacity, uppercase=uppercase,
    )

    is_dubbed = filename.startswith("translated_")

    if is_dubbed:
        if is_karaoke:
            success = generate_srt_from_video(input_path, srt_path, style="karaoke", **karaoke_opts)
        else:
            success = generate_srt_from_video(input_path, srt_path)
    elif is_karaoke:
        if not transcript:
            raise ProcessorError("No transcript for karaoke subtitles", "subtitle")
        success = generate_ass(transcript, clip_data["start"], clip_data["end"],
                               srt_path, **karaoke_opts)
    else:
        if not transcript:
            raise ProcessorError("No transcript for subtitles", "subtitle")
        success = generate_srt(transcript, clip_data["start"], clip_data["end"], srt_path)

    if not success:
        raise ProcessorError("No words found for this clip range", "subtitle")

    output_filename = f"subtitled_{generation_id}_{filename}"
    output_path = os.path.join(output_dir, output_filename)

    burn_subtitles(input_path, srt_path, output_path,
                   alignment=position, fontsize=font_size,
                   font_name=font_name, font_color=font_color,
                   border_color=border_color, border_width=border_width,
                   bg_color=bg_color, bg_opacity=bg_opacity)

    return output_filename


def _apply_hook(job_id: str, clip_index: int, config: dict,
                output_dir: str) -> str:
    """Burn a viral hook text overlay onto a clip. Returns new filename."""
    from hooks import add_hook_to_video

    filename = resolve_clip_filename(job_id, clip_index, output_dir,
                                    config.get("input_filename"))

    # Re-applying a hook replaces the previous overlay instead of stacking a
    # second one: walk back leading hook_ prefixes to the pre-hook file (only
    # while that base still exists), mirroring the subtitle walk-back above.
    while True:
        m = re.match(r"^hook_(.+)$", filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            break
        filename = m.group(1)

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise ProcessorError(f"Video file not found: {filename}", "hook")

    text = config.get("text", "")
    if not text:
        meta = get_clip_metadata(job_id, clip_index, output_dir)
        text = meta["clip_data"].get("viral_hook_text", "")
    if not text:
        raise ProcessorError("No hook text provided or found in metadata", "hook")

    position = config.get("position", "top")
    size = config.get("size", "M")
    duration_seconds = config.get("duration_seconds")
    style = config.get("style", "classic")

    size_map = {"S": 0.8, "M": 1.0, "L": 1.3}
    font_scale = size_map.get(size, 1.0)

    output_filename = f"hook_{filename}"
    output_path = os.path.join(output_dir, output_filename)

    add_hook_to_video(input_path, text, output_path,
                      position=position, font_scale=font_scale,
                      duration=duration_seconds, style=style)

    return output_filename


def _apply_auto_edit(job_id: str, clip_index: int, config: dict,
                     output_dir: str) -> str:
    """Apply AI auto-edit (Gemini plan + FFmpeg re-encode). Returns new filename."""
    from editor import VideoEditor
    import cv2

    api_key = config.get("api_key")
    if not api_key:
        raise ProcessorError("No Gemini API key for auto edit", "auto_edit")

    filename = resolve_clip_filename(job_id, clip_index, output_dir,
                                    config.get("input_filename"))
    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise ProcessorError(f"Video file not found: {filename}", "auto_edit")

    edited_filename = f"edited_{filename}"
    output_path = os.path.join(output_dir, edited_filename)

    editor = VideoEditor(api_key=api_key)

    safe_input = os.path.join(output_dir, f"temp_batch_in_{job_id}_{clip_index}.mp4")
    safe_output = os.path.join(output_dir, f"temp_batch_out_{job_id}_{clip_index}.mp4")
    shutil.copy(input_path, safe_input)

    try:
        vid_file = editor.upload_video(safe_input)

        cap = cv2.VideoCapture(safe_input)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = frame_count / fps if fps else 0
        cap.release()

        transcript = None
        try:
            meta = get_clip_metadata(job_id, clip_index, output_dir)
            transcript = meta.get("transcript")
        except Exception:
            pass

        has_captions = ("subtitled_" in filename) or ("hook_" in filename)
        filter_data = editor.get_ffmpeg_filter(
            vid_file, duration, fps=fps, width=width, height=height,
            transcript=transcript, has_captions=has_captions)

        editor.apply_edits(safe_input, safe_output, filter_data)

        if os.path.exists(safe_output):
            shutil.move(safe_output, output_path)
        else:
            raise ProcessorError("Auto edit produced no output", "auto_edit")
    finally:
        if os.path.exists(safe_input):
            os.remove(safe_input)
        if os.path.exists(safe_output):
            os.remove(safe_output)

    return edited_filename


def _apply_branding(job_id: str, clip_index: int, config: dict,
                    output_dir: str) -> str:
    """Apply branding (logo overlay) from render options. Returns new filename."""
    from render_options import load_options, resolve_options, apply_branding, rebrand_clip

    filename = resolve_clip_filename(job_id, clip_index, output_dir,
                                    config.get("input_filename"))
    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise ProcessorError(f"Video file not found: {filename}", "branding")

    defaults, overrides = load_options(output_dir)
    clip_override = overrides.get(str(clip_index))
    effective = resolve_options(defaults, clip_override)

    if not effective.branding.logo.enabled:
        return filename

    base_name = filename.rsplit("_clip_", 1)[0] if "_clip_" in filename else filename.replace(".mp4", "")
    pre_brand_path = os.path.join(output_dir, f"{base_name}_clip_{clip_index + 1}_pre_brand.mp4")

    if os.path.exists(pre_brand_path):
        rebrand_clip(pre_brand_path, input_path, effective.branding, output_dir)
    else:
        apply_branding(input_path, effective.branding, output_dir)

    return filename


def _apply_translate(job_id: str, clip_index: int, config: dict,
                     output_dir: str) -> str:
    """Translate/dub a clip via ElevenLabs. Returns new filename."""
    from translate import translate_video

    api_key = config.get("api_key")
    if not api_key:
        raise ProcessorError("No ElevenLabs API key for translate", "translate")

    target_language = config.get("target_language")
    if not target_language:
        raise ProcessorError("No target_language specified", "translate")

    source_language = config.get("source_language")

    filename = resolve_clip_filename(job_id, clip_index, output_dir,
                                    config.get("input_filename"))
    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise ProcessorError(f"Video file not found: {filename}", "translate")

    base, ext = os.path.splitext(filename)
    output_filename = f"translated_{target_language}_{base}{ext}"
    output_path = os.path.join(output_dir, output_filename)

    translate_video(
        video_path=input_path,
        output_path=output_path,
        target_language=target_language,
        api_key=api_key,
        source_language=source_language,
    )

    return output_filename


# ---------------------------------------------------------------------------
# Operation Registry
# ---------------------------------------------------------------------------

class OperationDef:
    __slots__ = ("processor", "resource", "label")

    def __init__(self, processor: Callable, resource: str, label: str):
        self.processor = processor
        self.resource = resource
        self.label = label


OPERATIONS: Dict[str, OperationDef] = {
    "subtitle": OperationDef(
        processor=_apply_subtitle,
        resource="ffmpeg",
        label="Burning subtitles",
    ),
    "hook": OperationDef(
        processor=_apply_hook,
        resource="ffmpeg",
        label="Adding viral hook",
    ),
    "auto_edit": OperationDef(
        processor=_apply_auto_edit,
        resource="gemini",
        label="Applying auto edit",
    ),
    "branding": OperationDef(
        processor=_apply_branding,
        resource="ffmpeg",
        label="Applying branding",
    ),
    "translate": OperationDef(
        processor=_apply_translate,
        resource="elevenlabs",
        label="Translating audio",
    ),
}

RESOURCE_CONCURRENCY = {
    "ffmpeg": 3,
    "gemini": 2,
    "elevenlabs": 1,
}


# ---------------------------------------------------------------------------
# Batch Orchestrator
# ---------------------------------------------------------------------------

class BatchProgress:
    """Thread-safe progress tracker stored on the job dict."""

    def __init__(self, total_clips: int, operations: List[str]):
        self.status = "running"
        self.operations = operations
        self.total_clips = total_clips
        self.completed_clips = 0
        self.failed_clips: List[dict] = []
        self.current_clip = 0
        self.current_step = ""
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "operations": self.operations,
                "total": self.total_clips,
                "completed": self.completed_clips,
                "failed": list(self.failed_clips),
                "current_clip": self.current_clip,
                "current_step": self.current_step,
            }

    def set_current(self, clip_index: int, step: str):
        with self._lock:
            self.current_clip = clip_index
            self.current_step = step

    def mark_completed(self):
        with self._lock:
            self.completed_clips += 1

    def mark_failed(self, clip_index: int, step: str, error: str):
        with self._lock:
            self.failed_clips.append({
                "clip_index": clip_index,
                "step": step,
                "error": error[:200],
            })

    def finish(self):
        with self._lock:
            self.status = "cancelled" if self.cancel_event.is_set() else "completed"

    def cancel(self):
        self.cancel_event.set()


def run_batch(job_id: str, clip_indices: List[int], operations: List[dict],
              output_dir: str, jobs: dict, archive_fn=None):
    """Execute the batch pipeline. Runs in a background thread.

    operations: [{"type": "subtitle", "config": {...}}, ...]
    """
    op_types = [op["type"] for op in operations]
    progress = BatchProgress(len(clip_indices), op_types)
    jobs[job_id]["batch"] = progress

    max_workers = max(
        RESOURCE_CONCURRENCY.get(OPERATIONS[op["type"]].resource, 1)
        for op in operations
    ) if operations else 1
    max_workers = min(max_workers, int(os.environ.get("CLIP_WORKERS", "3")))

    def process_one_clip(clip_index: int):
        if progress.cancel_event.is_set():
            return

        current_filename = None
        for op in operations:
            if progress.cancel_event.is_set():
                return

            op_type = op["type"]
            op_def = OPERATIONS[op_type]
            config = dict(op.get("config", {}))

            if current_filename:
                config["input_filename"] = current_filename

            progress.set_current(clip_index, op_def.label)

            try:
                new_filename = op_def.processor(job_id, clip_index, config, output_dir)
                current_filename = new_filename
                persist_video_url(job_id, clip_index, new_filename, output_dir, jobs)
                if archive_fn:
                    try:
                        archive_fn(job_id, clip_index, new_filename)
                    except Exception:
                        pass  # archiving is best-effort
            except BatchCancelled:
                return
            except Exception as e:
                progress.mark_failed(clip_index, op_type, str(e))
                print(f"⚠️ batch: clip {clip_index} failed at {op_type}: {e}")
                return

        progress.mark_completed()

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(process_one_clip, idx): idx
                for idx in clip_indices
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    idx = futures[future]
                    progress.mark_failed(idx, "unknown", str(e))
    finally:
        progress.finish()
