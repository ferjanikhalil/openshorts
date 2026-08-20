"""Making a clip reachable by the provider.

Status 200 ingests media **by URL**: you hand it a public link and it downloads
the bytes itself. That single fact drives this whole module. A clip sitting on a
local disk behind a NAT is unpublishable no matter how correct the rest of the
pipeline is, so reachability is a first-class precondition, checked at boot and
again per request.

Three strategies, chosen by what the deploy actually has:

  R2 presigned (cloud)   Already how the durable library serves private video.
                         Short-lived, unguessable, no new attack surface.
  Object store           Any S3-compatible bucket (``objectstore.py``). The clip
  presigned              is copied there in the background ahead of its slot and
                         the provider gets a presigned GET. This is the only
                         strategy whose speed does not depend on THIS machine's
                         uplink, which is what makes publishing viable from a
                         home connection at all — see objectstore's docstring.
  Signed token           A ``GET /api/publishing/media/{token}`` route serving
  (self-host)            one clip, gated by an HMAC that pins job/clip/filename
                         and an expiry. The media directory is NOT exposed.
                         Correct, but the provider then downloads the whole clip
                         from here, inside its own submit request.

None of them makes a clip reachable if the deploy has no public ingress and no
store. That case is detected and reported rather than discovered as a 422 from
the provider three hours into a scheduled run.
"""
import asyncio
import os
from typing import Optional, Tuple

from .config import settings
from . import signing, state

# Path segment for the self-host signed-media route. Kept here so the route and
# the URL builder cannot drift apart.
MEDIA_ROUTE_PREFIX = "/api/publishing/media"

# Returned as the "strategy" when the object store is the chosen origin but the
# clip has not finished being copied there. NOT an error: the transfer loop is
# working on it, and the caller's job is to wait, not to fail. Callers test with
# ``is_pending`` because a reason is appended after a colon.
STRATEGY_PENDING = "objectstore_pending"


def is_pending(strategy: str) -> bool:
    return bool(strategy) and strategy.split(":", 1)[0] == STRATEGY_PENDING


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


def store_available() -> bool:
    try:
        from . import objectstore
        return objectstore.configured()
    except Exception:
        return False


def store_key_for(job_id: str, clip_index: int, fingerprint: str) -> str:
    from . import objectstore
    return objectstore.object_key(job_id, clip_index, fingerprint)


def media_strategy() -> str:
    """Which origin this deploy would use. Must mirror ``public_url_for_clip``.

    Single source of truth for the two health endpoints, which each used to
    inline their own copy of this ladder.
    """
    if r2_available():
        return "r2_presigned"
    if store_available():
        return "objectstore_presigned"
    if settings.public_base_url:
        return "signed_token"
    return "none"


def reachability_warnings() -> list:
    """Boot diagnostics. Publishing cannot work if media is unreachable."""
    out = []
    datacenter_origin = r2_available() or store_available()
    if datacenter_origin:
        # Media is served from object storage, so public_base_url is no longer
        # needed for clips — but it is still the only way to hand the provider a
        # webhook URL, and a post nobody confirms ages into `unknown`.
        if not settings.public_base_url:
            out.append(
                "Clips are served from object storage (good), but "
                "PUBLISHING_PUBLIC_BASE_URL / FRONTEND_URL is unset, so there is "
                "no webhook callback URL to register with the provider. Posts "
                "would go out and never be confirmed."
            )
        return out
    base = settings.public_base_url
    if not base:
        out.append(
            "Publishing has no public media origin: no object store is "
            "configured (PUBLISHING_S3_*), R2 is not configured, and "
            "PUBLISHING_PUBLIC_BASE_URL / FRONTEND_URL is not set. The provider "
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


async def public_url_for_clip(job_id: str, clip_index: int, filename: str, *,
                              fingerprint: Optional[str] = None,
                              user_id=None, ttl_seconds: Optional[int] = None
                              ) -> Tuple[Optional[str], str]:
    """Build a provider-fetchable URL for one clip.

    Returns ``(url, strategy)``; ``(None, reason)`` when the deploy cannot expose
    media at all, and ``(None, STRATEGY_PENDING…)`` when the store is the origin
    and the clip is still being copied there — a wait, not a failure.

    Async because the store branch has to ask whether the object is actually
    there. That is one HEAD against a datacenter, off the event loop; the
    presigning itself is local arithmetic.

    Order is by cost of the bytes already in place: in cloud mode R2 *is* the
    library, so no copy is needed; a configured store is next; the signed-token
    route last, because it makes the provider pull the whole clip from here.
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

    if store_available() and fingerprint:
        from . import objectstore
        key = objectstore.object_key(job_id, clip_index, fingerprint)
        try:
            present = await asyncio.to_thread(objectstore.head, key)
            if present:
                return (await asyncio.to_thread(
                    objectstore.presigned_get, key, expires=ttl),
                    "objectstore_presigned")
            return None, f"{STRATEGY_PENDING}: the clip is not in the media " \
                         f"store yet"
        except objectstore.StoreError as e:
            # Deliberately parked rather than degraded to the slow route. A
            # broken store is an operator problem with a visible reason; falling
            # back would hand the provider a URL this uplink cannot serve in
            # time, and a timed-out submit is ambiguous — `unknown` forever.
            return None, f"{STRATEGY_PENDING}: media store unreachable ({e})"

    base = settings.public_base_url
    if not base:
        return None, ("no public media origin configured "
                      "(set PUBLISHING_S3_* for an object store, or "
                      "PUBLISHING_PUBLIC_BASE_URL, or configure R2)")
    import time
    token = signing.sign_media_token(
        _media_signing_secret(), job_id, clip_index, filename,
        int(time.time()) + ttl)
    return f"{base}{MEDIA_ROUTE_PREFIX}/{token}", "signed_token"


def verify_media_request(token: str) -> Tuple[bool, dict, str]:
    """Validate an inbound signed media token. Used by the media route."""
    return signing.verify_media_token(_media_signing_secret(), token)
