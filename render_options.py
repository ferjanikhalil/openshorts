"""Render Options: unified job-level rendering configuration.

Architecture:
- RenderOptions (Pydantic) is the container for all pre-configurable render modules.
- Each module (branding, subtitles, hook, etc.) is an independent sub-model.
- Per-clip overrides are sparse dicts (only modified fields).
- resolve_options() merges job default + override into a final RenderOptions.
- The rendering pipeline reads RenderOptions and applies enabled modules in order.

Backward compatibility:
- Reads branding.json if render_options.json doesn't exist (migration path).
- Old function names (load_branding, save_branding, resolve_branding) are aliases.
"""

import os
import json
import shutil
import subprocess
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from ffmpeg_utils import video_encode_args, QUALITY


# ---- Module: Branding (logo overlay) -------------------------------------------

class LogoOverlay(BaseModel):
    enabled: bool = False
    image_path: Optional[str] = None
    position: str = "bottom_right"  # top_left | top_right | bottom_left | bottom_right
    size_pct: float = Field(default=15.0, ge=1.0, le=60.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    margin_px: int = Field(default=20, ge=0, le=500)


class BrandingConfig(BaseModel):
    logo: LogoOverlay = Field(default_factory=LogoOverlay)


# ---- Module: Subtitles (style only — content comes from transcript) -------------

class SubtitleStyle(BaseModel):
    enabled: bool = False
    style: str = "classic"  # classic | karaoke
    position: str = "bottom"  # top | middle | bottom
    font_name: str = "Verdana"
    font_size: int = 12
    font_color: str = "#FFFFFF"
    border_color: str = "#000000"
    border_width: int = 2
    bg_color: Optional[str] = None
    bg_opacity: float = 0.0
    highlight_color: str = "#FFD700"
    effect: str = "none"  # none | glow | pop | box
    base_opacity: float = 1.0
    uppercase: bool = False


# ---- Module: Hook (style only — text comes from LLM) ----------------------------

class HookStyle(BaseModel):
    enabled: bool = False
    style: str = "classic"  # classic | dark | yellow | red | outline | outline_yellow
    position: str = "top"  # top | center | bottom
    size: str = "M"  # S | M | L
    duration_seconds: Optional[float] = 5.0


# ---- Module: Auto Edit (AI-driven, job-level enable only) -----------------------

class AutoEditConfig(BaseModel):
    enabled: bool = False


# ---- Module: Translate (AI voice dubbing — style/target only) -------------------
# The manual per-clip path takes target_language from the TranslateModal at edit
# time; a recipe needs to pin it upfront so autopilot can dub unattended. There is
# no home for this on RenderOptions today, so this sub-model is net-new.

class TranslateConfig(BaseModel):
    enabled: bool = False
    target_language: Optional[str] = None
    source_language: Optional[str] = None


# ---- Render Options container ---------------------------------------------------

class RenderOptions(BaseModel):
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    subtitles: Optional[SubtitleStyle] = None
    hook: Optional[HookStyle] = None
    auto_edit: Optional[AutoEditConfig] = None
    translate: Optional[TranslateConfig] = None


# ---- Inheritance / resolution ---------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins on present keys)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def resolve_options(default: RenderOptions, override: Optional[dict]) -> RenderOptions:
    """Merge job default RenderOptions with a sparse per-clip override dict."""
    if not override:
        return default
    base = default.model_dump()
    deep_merge(base, override)
    return RenderOptions.model_validate(base)


def resolve_branding(default: BrandingConfig, override: Optional[dict]) -> BrandingConfig:
    """Legacy: merge job default branding with a sparse per-clip override dict."""
    if not override:
        return default
    base = default.model_dump()
    deep_merge(base, override)
    return BrandingConfig.model_validate(base)


# ---- Cascade + operation compilation (autopilot) --------------------------------
# render_options_to_operations() is the FIRST reader of subtitles/hook/auto_edit/
# translate. Until now only branding was applied downstream; these fields were
# persisted but inert. Adding a reader here cannot regress existing behavior
# because nothing else consumes them.

def resolve_cascade(batch_default: RenderOptions, video_override: Optional[dict]) -> RenderOptions:
    """Merge a batch-level default with a sparse per-video override dict.

    This is the batch->video level of autopilot's 3-level cascade
    (batch default -> per-video override -> per-clip override). It is resolved
    ONCE at job creation and baked into the child job's render_options.json
    `default`, so the existing 2-level (default->clip) hot path in
    resolve_options() stays completely untouched.

    Returns a FRESH RenderOptions every time (never the shared batch_default),
    so baking it into a child job can't later mutate the batch recipe.

    None-submodule note: batch_default.model_dump() already materializes enabled
    sub-models to plain dicts (a disabled/absent module dumps to None), so
    deep_merge's "recurse only when both sides are dicts" rule does the right
    thing — a video can both tweak an inherited module and turn on one the batch
    left off.
    """
    base = batch_default.model_dump()
    if video_override:
        deep_merge(base, video_override)
    return RenderOptions.model_validate(base)


# Canonical per-clip operation order. Implicit in BatchPipeline.jsx's OPERATIONS
# list today; this is the single source of truth once recipes compile server-side.
_OPERATION_ORDER = ("auto_edit", "subtitle", "hook", "branding", "translate")


def render_options_to_operations(resolved: RenderOptions, *, gemini_key: Optional[str] = None,
                                 elevenlabs_key: Optional[str] = None) -> list:
    """Compile a resolved RenderOptions into a batch `operations` list.

    Emits [{"type": ..., "config": {...}}, ...] in canonical order, gating each
    op on its module's `enabled` flag (the batch processors themselves ignore
    `enabled`, so the gate must live here). The style sub-model field names are
    already the snake_case keys the processors read, so config is the sub-model
    dump minus `enabled`, with None values dropped so processor defaults apply —
    mirroring what the manual modal path omits.
    """
    ops = []

    def _style_cfg(model) -> dict:
        return {k: v for k, v in model.model_dump().items()
                if k != "enabled" and v is not None}

    for op_type in _OPERATION_ORDER:
        if op_type == "auto_edit":
            if resolved.auto_edit and resolved.auto_edit.enabled:
                ops.append({"type": "auto_edit", "config": {"api_key": gemini_key}})
        elif op_type == "subtitle":
            # Field name on the model is `subtitles`; op type is singular `subtitle`.
            if resolved.subtitles and resolved.subtitles.enabled:
                ops.append({"type": "subtitle", "config": _style_cfg(resolved.subtitles)})
        elif op_type == "hook":
            # No `text` field on HookStyle -> processor falls back to each clip's
            # AI-generated viral_hook_text, which is what a recipe wants.
            if resolved.hook and resolved.hook.enabled:
                ops.append({"type": "hook", "config": _style_cfg(resolved.hook)})
        elif op_type == "branding":
            # Branding processor self-loads render_options.json from disk.
            if resolved.branding and resolved.branding.logo.enabled:
                ops.append({"type": "branding", "config": {}})
        elif op_type == "translate":
            t = resolved.translate
            if t and t.enabled and t.target_language:
                cfg = {"api_key": elevenlabs_key, "target_language": t.target_language}
                if t.source_language:
                    cfg["source_language"] = t.source_language
                ops.append({"type": "translate", "config": cfg})

    return ops


# ---- Persistence ----------------------------------------------------------------

def load_options(job_dir: str) -> tuple:
    """Load render_options.json (or legacy branding.json) from a job directory.

    Returns (RenderOptions, clip_overrides_dict).
    """
    path = os.path.join(job_dir, "render_options.json")
    legacy_path = os.path.join(job_dir, "branding.json")

    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            default = RenderOptions.model_validate(data.get("default", {}))
            overrides = data.get("clip_overrides", {})
            return default, overrides
        except Exception as e:
            print(f"   ⚠️ Could not load render_options.json ({e}); using defaults.")
            return RenderOptions(), {}

    # Backward compat: read legacy branding.json
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, "r") as f:
                data = json.load(f)
            branding = BrandingConfig.model_validate(data.get("default", {}))
            overrides = data.get("clip_overrides", {})
            return RenderOptions(branding=branding), overrides
        except Exception as e:
            print(f"   ⚠️ Could not load branding.json ({e}); using defaults.")

    return RenderOptions(), {}


def save_options(job_dir: str, default: RenderOptions, overrides: Dict[str, Any]):
    """Persist render options + overrides to the job directory."""
    path = os.path.join(job_dir, "render_options.json")
    data = {
        "default": default.model_dump(),
        "clip_overrides": overrides,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# Legacy aliases
def load_branding(job_dir: str) -> tuple:
    """Legacy: returns (BrandingConfig, overrides) for backward compat."""
    options, overrides = load_options(job_dir)
    return options.branding, overrides


def save_branding(job_dir: str, default: BrandingConfig, overrides: Dict[str, Any]):
    """Legacy: saves branding config. Wraps in RenderOptions for forward compat."""
    options = RenderOptions(branding=default)
    save_options(job_dir, options, overrides)


# ---- Branding renderer ----------------------------------------------------------

def _probe_dimensions(video_path: str) -> tuple:
    """Return (width, height) of the first video stream."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", video_path],
        stderr=subprocess.STDOUT, timeout=60,
    ).decode().strip().split("x")
    return int(out[0]), int(out[1])


def _overlay_position(position: str, vw: int, vh: int, logo_w: int, logo_h: int, margin: int) -> tuple:
    """Compute (x, y) for the overlay given a named corner position."""
    if position == "top_left":
        return margin, margin
    elif position == "top_right":
        return vw - logo_w - margin, margin
    elif position == "bottom_left":
        return margin, vh - logo_h - margin
    else:  # bottom_right
        return vw - logo_w - margin, vh - logo_h - margin


def apply_branding(video_path: str, config: BrandingConfig, job_dir: str) -> bool:
    """Burn the logo overlay onto a video file (single FFmpeg pass).

    Modifies video_path in place (write to temp, then replace).
    Returns True on success, False if branding was skipped or failed.
    """
    logo_cfg = config.logo
    if not logo_cfg.enabled or not logo_cfg.image_path:
        return False

    logo_path = logo_cfg.image_path
    if not os.path.isabs(logo_path):
        logo_path = os.path.join(job_dir, logo_path)
    if not os.path.exists(logo_path):
        print(f"   ⚠️ Branding logo not found ({logo_path}); clip kept unbranded.")
        return False

    try:
        vw, vh = _probe_dimensions(video_path)
    except Exception as e:
        print(f"   ⚠️ Could not probe video for branding ({e}); clip kept unbranded.")
        return False

    logo_w = max(40, int(vw * logo_cfg.size_pct / 100.0))

    try:
        logo_dims = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", logo_path],
            stderr=subprocess.STDOUT, timeout=30,
        ).decode().strip().split("x")
        logo_aspect = int(logo_dims[0]) / max(1, int(logo_dims[1]))
    except Exception:
        logo_aspect = 1.0

    logo_h = max(20, int(logo_w / logo_aspect))
    x, y = _overlay_position(logo_cfg.position, vw, vh, logo_w, logo_h, logo_cfg.margin_px)

    opacity_filter = f"colorchannelmixer=aa={logo_cfg.opacity}" if logo_cfg.opacity < 1.0 else ""
    scale_and_format = f"[1:v]scale={logo_w}:-1,format=rgba"
    if opacity_filter:
        scale_and_format += f",{opacity_filter}"
    scale_and_format += "[logo]"

    filt = f"{scale_and_format};[0:v][logo]overlay=x={x}:y={y}"

    tmp_path = video_path + ".brand_tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", video_path, "-i", logo_path,
        "-filter_complex", filt,
        *video_encode_args(QUALITY), "-c:a", "copy",
        "-movflags", "+faststart", tmp_path,
    ]

    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        if result.returncode == 0 and os.path.exists(tmp_path):
            os.replace(tmp_path, video_path)
            return True
        err = (result.stderr or b"").decode(errors="ignore")[-300:]
        print(f"   ⚠️ Branding pass failed (clip kept unbranded): {err}")
    except Exception as e:
        print(f"   ⚠️ Branding pass error ({e}); clip kept unbranded.")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return False


def rebrand_clip(pre_brand_path: str, output_path: str, config: BrandingConfig, job_dir: str) -> bool:
    """Re-render branding from a pre-brand intermediate onto the output path."""
    logo_cfg = config.logo
    if not logo_cfg.enabled or not logo_cfg.image_path:
        shutil.copy2(pre_brand_path, output_path)
        return True

    logo_path = logo_cfg.image_path
    if not os.path.isabs(logo_path):
        logo_path = os.path.join(job_dir, logo_path)
    if not os.path.exists(logo_path):
        shutil.copy2(pre_brand_path, output_path)
        return True

    try:
        vw, vh = _probe_dimensions(pre_brand_path)
    except Exception:
        shutil.copy2(pre_brand_path, output_path)
        return True

    logo_w = max(40, int(vw * logo_cfg.size_pct / 100.0))

    try:
        logo_dims = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", logo_path],
            stderr=subprocess.STDOUT, timeout=30,
        ).decode().strip().split("x")
        logo_aspect = int(logo_dims[0]) / max(1, int(logo_dims[1]))
    except Exception:
        logo_aspect = 1.0

    logo_h = max(20, int(logo_w / logo_aspect))
    x, y = _overlay_position(logo_cfg.position, vw, vh, logo_w, logo_h, logo_cfg.margin_px)

    opacity_filter = f"colorchannelmixer=aa={logo_cfg.opacity}" if logo_cfg.opacity < 1.0 else ""
    scale_and_format = f"[1:v]scale={logo_w}:-1,format=rgba"
    if opacity_filter:
        scale_and_format += f",{opacity_filter}"
    scale_and_format += "[logo]"

    filt = f"{scale_and_format};[0:v][logo]overlay=x={x}:y={y}"

    cmd = [
        "ffmpeg", "-y", "-i", pre_brand_path, "-i", logo_path,
        "-filter_complex", filt,
        *video_encode_args(QUALITY), "-c:a", "copy",
        "-movflags", "+faststart", output_path,
    ]

    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        err = (result.stderr or b"").decode(errors="ignore")[-300:]
        print(f"   ⚠️ Re-brand failed: {err}")
    except Exception as e:
        print(f"   ⚠️ Re-brand error: {e}")
    return False
