"""The seam between publishing and the video pipeline.

Publishing must not import ``app`` — ``app`` imports publishing, and the job
store lives in ``app``'s memory. So instead of reaching into it, publishing
declares the one question it needs answered ("where are this clip's bytes, and
what should its caption be?") and ``app`` registers a resolver at startup.

Same shape as ``batch.run_batch(..., archive_fn=...)``: the caller injects the
capability rather than the library discovering it.

A resolver returns, or None if the clip is gone:

    {
      "output_dir":  str,          # base output directory
      "filename":    str,          # CURRENT filename (mutates as ops run)
      "user_id":     Any | None,   # owner, for R2 presigning
      "title":       str,
      "caption":     str,
      "duration":    float | None,
      "size_bytes":  int | None,
      "mtime":       float | None,
      "fingerprint": str,
      # Optional. The pipeline already writes platform-specific text (a YouTube
      # title, TikTok and Instagram descriptions); surfacing it here keeps a
      # fan-out from posting the same generic caption to all three.
      "per_platform": {"<platform>": {"title": str, "caption": str}},
    }

Clips expire: ``JOB_RETENTION_SECONDS`` deletes files 24 h after a job finishes,
which is well inside the window a scheduled post can span. A resolver returning
None is therefore a normal outcome, not an error, and the dispatcher treats it as
a permanent failure with a clear reason rather than retrying against a file that
will never come back.
"""
from typing import Callable, Optional

_resolver: Optional[Callable] = None


def set_resolver(fn: Callable) -> None:
    """Register the clip resolver. Called once from ``app`` at startup."""
    global _resolver
    _resolver = fn


def has_resolver() -> bool:
    return _resolver is not None


def resolve(job_id: str, clip_index: int) -> Optional[dict]:
    """Look up a clip, or None when it cannot be resolved."""
    if _resolver is None:
        return None
    try:
        return _resolver(job_id, clip_index)
    except Exception as e:
        print(f"⚠️  Publishing: clip resolver failed for "
              f"{job_id}[{clip_index}]: {e}")
        return None


def build_caption(clip_info: dict, payload: Optional[dict],
                  platform: str) -> tuple:
    """Resolve the (title, caption) actually sent for one platform.

    Precedence: the request's per-platform override, then its request-level text,
    then the clip's own platform-specific text, then the clip's generic text.
    What the operator typed always outranks what the pipeline generated, and a
    platform-specific value always outranks a generic one at the same level.

    Resolved at submit time from the request payload — which was itself frozen at
    request creation — so editing a template later never rewrites a live post.
    """
    from . import platforms as plat
    payload = payload or {}
    p = plat.normalize(platform)
    per_platform = (payload.get("per_platform") or {}).get(p) or {}
    clip_platform = (clip_info.get("per_platform") or {}).get(p) or {}

    title = (per_platform.get("title") or payload.get("title")
             or clip_platform.get("title") or clip_info.get("title") or "")
    caption = (per_platform.get("caption") or payload.get("caption")
               or clip_platform.get("caption") or clip_info.get("caption")
               or title or "")

    limit = plat.caption_limit(p)
    if limit and len(caption) > limit:
        # Truncate rather than fail: a caption two chars over the limit should
        # not cost a publication. The event log records that it happened.
        caption = caption[:limit - 1].rstrip() + "…"
    return title, caption
