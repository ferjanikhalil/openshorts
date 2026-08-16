"""Making a clip reachable by the provider.

Status 200 ingests media **by URL**: you hand it a public link and it downloads
the bytes itself. That single fact drives this whole module. A clip sitting on a
local disk behind a NAT is unpublishable no matter how correct the rest of the
pipeline is, so reachability is a first-class precondition, checked at boot and
again per request.

Two strategies, chosen by what the deploy actually has:

  R2 presigned (cloud)   Already how the durable library serves private video.
                         Short-lived, unguessable, no new attack surface.
  Signed token           A ``GET /api/publishing/media/{token}`` route serving
  (self-host)            one clip, gated by an HMAC that pins job/clip/filename
                         and an expiry. The media directory is NOT exposed.

Neither strategy makes a clip reachable if the deploy has no public ingress at
all. That case is detected and reported rather than discovered as a 422 from the
provider three hours into a scheduled run.
"""
import os
from typing import Optional, Tuple

from .config import settings
from . import signing, state

# Path segment for the self-host signed-media route. Kept here so the route and
# the URL builder cannot drift apart.
MEDIA_ROUTE_PREFIX = "/api/publishing/media"


def _media_signing_secret() -> str:
    """Secret for media tokens.

    Derived from the master key rather than configured separately: one fewer
    thing to set, and a rotated master key correctly invalidates outstanding
    media URLs. Domain-separated so a media token can never be confused with a
    credential blob.
    """
    import hashlib
    from .config import decode_master_key
    key = decode_master_key(settings.master_key_b64)
    return hashlib.sha256(b"openshorts-publishing-media\x00" + key).hexdigest()


def r2_available() -> bool:
    try:
        from cloud.config import settings as cloud_settings
        return bool(cloud_settings.r2_configured)
    except Exception:
        return False


def reachability_warnings() -> list:
    """Boot diagnostics. Publishing cannot work if media is unreachable."""
    out = []
    if r2_available():
        return out
    base = settings.public_base_url
    if not base:
        out.append(
            "Publishing has no public media origin: neither R2 is configured nor "
            "PUBLISHING_PUBLIC_BASE_URL / FRONTEND_URL is set. The provider "
            "downloads clips by URL, so no post can succeed until one of them is "
            "configured."
        )
    elif base.startswith("http://localhost") or base.startswith("http://127."):
        out.append(
            f"PUBLISHING_PUBLIC_BASE_URL is {base}, which the provider cannot "
            "reach from the internet. Set it to the deployment's public origin."
        )
    elif base.startswith("http://"):
        out.append(
            f"PUBLISHING_PUBLIC_BASE_URL is plain HTTP ({base}). Media tokens "
            "would travel unencrypted; use HTTPS."
        )
    return out


def clip_local_path(output_dir: str, job_id: str, filename: str) -> Optional[str]:
    """Resolve a clip path, refusing anything that escapes the job directory.

    Reuses the same realpath containment rule as ``app._safe_under``. The
    filename here comes from a job record rather than a client, but publishing
    turns it into a signed public URL, so the guard stays.
    """
    base = os.path.realpath(os.path.join(output_dir, job_id))
    target = os.path.realpath(os.path.join(base, filename))
    if target == base or target.startswith(base + os.sep):
        return target
    return None


def describe_clip(output_dir: str, job_id: str, clip_index: int,
                  clip: dict) -> dict:
    """Everything publishing needs to know about a clip's bytes.

    ``fingerprint`` folds in size and mtime, so re-styling a clip (which mutates
    the filename AND the bytes) produces a new identity and cannot reuse the
    provider media ref uploaded for the previous version.
    """
    video_url = (clip or {}).get("video_url") or ""
    filename = video_url.rstrip("/").split("/")[-1]
    info = {
        "job_id": job_id,
        "clip_index": clip_index,
        "filename": filename,
        "local_path": None,
        "size_bytes": None,
        "mtime": None,
        "duration_seconds": (clip or {}).get("duration"),
        "exists": False,
        "fingerprint": None,
    }
    if not filename:
        return info
    path = clip_local_path(output_dir, job_id, filename)
    info["local_path"] = path
    if path and os.path.exists(path):
        st = os.stat(path)
        info["exists"] = True
        info["size_bytes"] = st.st_size
        info["mtime"] = st.st_mtime
    info["fingerprint"] = state.content_fingerprint(
        job_id, clip_index, info["size_bytes"], info["mtime"])
    return info


def public_url_for_clip(job_id: str, clip_index: int, filename: str, *,
                        user_id=None, ttl_seconds: Optional[int] = None
                        ) -> Tuple[Optional[str], str]:
    """Build a provider-fetchable URL for one clip.

    Returns ``(url, strategy)``; ``(None, reason)`` when the deploy cannot
    expose media at all. Prefers R2 because in cloud mode the bytes are already
    there and presigned GETs are the established pattern.
    """
    ttl = ttl_seconds or settings.media_url_ttl_seconds

    if r2_available() and user_id is not None:
        try:
            from cloud import storage
            key = storage.job_key(user_id, job_id, filename)
            return storage.presigned_get(key, expires=ttl), "r2_presigned"
        except Exception as e:
            # Fall through to the signed-token route: a presign failure should
            # degrade, not abort the publish.
            print(f"⚠️  Publishing: R2 presign failed for {job_id}: {e}")

    base = settings.public_base_url
    if not base:
        return None, ("no public media origin configured "
                      "(set PUBLISHING_PUBLIC_BASE_URL or configure R2)")
    import time
    token = signing.sign_media_token(
        _media_signing_secret(), job_id, clip_index, filename,
        int(time.time()) + ttl)
    return f"{base}{MEDIA_ROUTE_PREFIX}/{token}", "signed_token"


def verify_media_request(token: str) -> Tuple[bool, dict, str]:
    """Validate an inbound signed media token. Used by the media route."""
    return signing.verify_media_token(_media_signing_secret(), token)
