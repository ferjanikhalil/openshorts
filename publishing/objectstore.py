"""A fast public media origin, backed by any S3-compatible object store.

Status 200 ingests media **by URL**, and it downloads the whole clip *inside*
the submit request. That makes this machine's upload bandwidth a hard
requirement of publishing rather than an implementation detail. Serving clips
directly off the box (``media.py``'s signed-token route) only works where the
box has datacenter-grade upstream: measured 2026-08-18 on a home line, a 21.7 MB
clip took 244 s to leave, the provider's gateway gave up first and returned a
bodyless ``504`` — which classifies as ``unknown``, is never auto-retried, and
therefore needs a human for every single post.

Staging the bytes in an object store inverts the problem. The slow transfer runs
once, in the background, ahead of the slot, on *our* clock (``worker`` gets a
transfer loop for exactly this). What the provider is handed is a presigned GET
against a datacenter, which completes in seconds no matter what this uplink can
do.

Deliberately store-agnostic — four env vars and four operations (PutObject,
HeadObject, presigned GetObject, DeleteObject), which is the intersection every
S3 API implements. Supabase Storage, Backblaze B2, Cloudflare R2, MinIO and AWS
S3 all satisfy it, and the free tiers of the first three are ample: the working
set is a handful of ~20 MB clips with a lifetime of hours.

Nothing here is required. With no store configured the module reports
``configured() is False`` and every caller keeps its previous behaviour.
"""
import os
import re
import threading
from collections import namedtuple
from typing import List, Optional
from urllib.parse import urlparse

# All objects live under one prefix, so publishing can share a bucket with
# something else and still be swept (or lifecycle-ruled) on its own.
KEY_PREFIX = "publishing"

# 5 MiB parts — S3's minimum, and the minimum is what you want here. Part size
# sets the wall clock of a single HTTP request: at the 16 KB/s this module exists
# to work around, 8 MB parts are 8.5 minutes each, which some storage gateways
# will cut off. Smaller parts also make a failed retry cheap. The 10,000-part
# ceiling still allows a 50 GB object.
PART_SIZE = 5 * 1024 * 1024

StoreConfig = namedtuple(
    "StoreConfig", "source endpoint bucket access_key secret_key region")

# Credential sets in preference order. Publishing's own vars win; the others let
# a deploy that already has object storage configured get a fast media origin
# without duplicating keys. (endpoint, bucket, access_key, secret_key, region)
_ENV_SETS = (
    ("PUBLISHING_S3", ("PUBLISHING_S3_ENDPOINT", "PUBLISHING_S3_BUCKET",
                       "PUBLISHING_S3_ACCESS_KEY_ID",
                       "PUBLISHING_S3_SECRET_ACCESS_KEY",
                       "PUBLISHING_S3_REGION")),
    ("R2", ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", None)),
    ("AWS", (None, "AWS_S3_BUCKET", "AWS_ACCESS_KEY_ID",
             "AWS_SECRET_ACCESS_KEY", "AWS_REGION")),
)

_client_lock = threading.Lock()
_cached_client = None
_cached_config = None
_boto3_present = None


class StoreError(RuntimeError):
    """The store is configured but did not answer.

    Always carries a scrubbed message: botocore puts the access key id into some
    signature errors, and a publishing error message ends up in the attempt row
    and the admin UI.
    """


def _env(name: Optional[str]) -> str:
    if not name:
        return ""
    return (os.environ.get(name) or "").strip()


def config() -> Optional[StoreConfig]:
    """Resolve the first fully-specified credential set, or None.

    Read on every call (no caching) for the same reason ``config.Settings`` is
    lazy: importing publishing must have no side effects, and tests set env vars
    after import.
    """
    for source, (endpoint, bucket, access, secret, region) in _ENV_SETS:
        b, a, s = _env(bucket), _env(access), _env(secret)
        if not (b and a and s):
            continue
        ep = _env(endpoint).rstrip("/")
        rg = _env(region)
        if not rg:
            # No endpoint means real AWS, which validates the region. Custom
            # endpoints mostly ignore it; "auto" is what R2 documents.
            rg = "us-east-1" if not ep else "auto"
        return StoreConfig(source, ep, b, a, s, rg)
    return None


def boto3_available() -> bool:
    global _boto3_present
    if _boto3_present is None:
        try:
            import importlib.util
            _boto3_present = importlib.util.find_spec("boto3") is not None
        except Exception:  # pragma: no cover - defensive
            _boto3_present = False
    return bool(_boto3_present)


def configured() -> bool:
    """True when this deploy can stage clips in an object store.

    Includes the boto3 check on purpose: a configured-but-unusable store would
    park every post forever with no visible cause, so "configured" has to mean
    "callable".
    """
    return boto3_available() and config() is not None


def describe() -> dict:
    """Diagnostics for the admin health endpoint. Never includes a key."""
    cfg = config()
    if cfg is None:
        return {"configured": False, "boto3": boto3_available()}
    return {
        "configured": boto3_available(),
        "boto3": boto3_available(),
        "source": cfg.source,
        "bucket": cfg.bucket,
        "endpoint": urlparse(cfg.endpoint).netloc if cfg.endpoint
                    else "s3.amazonaws.com",
        "region": cfg.region,
        "prefix": KEY_PREFIX,
    }


def object_key(job_id: str, clip_index: int, fingerprint: str) -> str:
    """Content-addressed key for one clip.

    The fingerprint (size + mtime, see ``media.describe_clip``) is *in* the key,
    which buys two things: re-styling a clip writes a different object, so a
    stale body can never be served under a live URL; and the key is derivable
    from a ``PublishMedia`` row or a queued attempt alone, so the sweeper can
    tell which objects are still needed without extra bookkeeping.
    """
    safe_job = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job_id))[:120] or "job"
    safe_fp = re.sub(r"[^A-Za-z0-9_.-]", "_", str(fingerprint))[:120] or "unknown"
    return f"{KEY_PREFIX}/{safe_job}/{int(clip_index)}/{safe_fp}.mp4"


def key_fingerprint(key: str) -> Optional[str]:
    """Recover the fingerprint from a key. Inverse of ``object_key``."""
    if not key.startswith(KEY_PREFIX + "/") or not key.endswith(".mp4"):
        return None
    parts = key[len(KEY_PREFIX) + 1:-4].split("/")
    return parts[-1] if len(parts) == 3 else None


def _safe_error(cfg: Optional[StoreConfig], exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    if cfg:
        for secret in (cfg.secret_key, cfg.access_key):
            if secret:
                msg = msg.replace(secret, "***")
    return msg[:400]


def _client(cfg: StoreConfig):
    global _cached_client, _cached_config
    with _client_lock:
        if _cached_client is not None and _cached_config == cfg:
            return _cached_client
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except Exception as e:  # pragma: no cover - requirements pin boto3
            raise StoreError(f"boto3 is not installed: {e}") from None
        boto_cfg = BotoConfig(
            signature_version="s3v4",
            # Path style is the only addressing every S3-compatible service
            # accepts (Supabase and MinIO require it); real AWS keeps its default.
            s3={"addressing_style": "path"} if cfg.endpoint else {},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=15,
            # Per-socket-read, not per-transfer: a multi-minute upload is fine,
            # a store that stops answering mid-part is not.
            read_timeout=300,
        )
        client = boto3.client(
            "s3", endpoint_url=cfg.endpoint or None,
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            region_name=cfg.region, config=boto_cfg)
        _cached_client, _cached_config = client, cfg
        return client


def _require() -> StoreConfig:
    cfg = config()
    if cfg is None:
        raise StoreError("no object store is configured")
    return cfg


def _is_missing(exc: Exception) -> bool:
    resp = getattr(exc, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code", ""))
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


# --- Operations (blocking; call from a thread) -------------------------------
def head(key: str) -> Optional[dict]:
    """Object metadata, or None when it is not there yet.

    Raises StoreError for anything that is not a clean 404 — a bad key or a
    missing bucket must not read as "still uploading".
    """
    cfg = _require()
    try:
        resp = _client(cfg).head_object(Bucket=cfg.bucket, Key=key)
    except Exception as e:
        if _is_missing(e):
            return None
        raise StoreError(_safe_error(cfg, e)) from None
    return {"size_bytes": resp.get("ContentLength"),
            "content_type": resp.get("ContentType")}


def upload(local_path: str, key: str, *,
           content_type: str = "video/mp4") -> int:
    """Put one clip in the store. Blocking, and on a slow uplink, slow.

    One part at a time (``max_concurrency=1``): parallel parts on a saturated
    uplink only compete with each other, the same reasoning behind the
    dispatcher's media pacer.
    """
    cfg = _require()
    try:
        from boto3.s3.transfer import TransferConfig
    except Exception as e:  # pragma: no cover
        raise StoreError(f"boto3 is not installed: {e}") from None
    transfer = TransferConfig(multipart_threshold=PART_SIZE,
                              multipart_chunksize=PART_SIZE,
                              max_concurrency=1, use_threads=True)
    try:
        _client(cfg).upload_file(
            local_path, cfg.bucket, key,
            ExtraArgs={"ContentType": content_type}, Config=transfer)
    except Exception as e:
        raise StoreError(_safe_error(cfg, e)) from None
    try:
        return os.path.getsize(local_path)
    except OSError:  # pragma: no cover - defensive
        return 0


def presigned_get(key: str, *, expires: int = 3600) -> str:
    """A URL the provider can fetch. Computed locally — no round trip."""
    cfg = _require()
    try:
        return _client(cfg).generate_presigned_url(
            "get_object", Params={"Bucket": cfg.bucket, "Key": key},
            ExpiresIn=int(expires))
    except Exception as e:
        raise StoreError(_safe_error(cfg, e)) from None


def delete(key: str) -> None:
    cfg = _require()
    try:
        _client(cfg).delete_object(Bucket=cfg.bucket, Key=key)
    except Exception as e:
        raise StoreError(_safe_error(cfg, e)) from None


def list_objects(limit: int = 1000) -> List[dict]:
    """One page of publishing's own objects, for the retention sweeper."""
    cfg = _require()
    try:
        resp = _client(cfg).list_objects_v2(
            Bucket=cfg.bucket, Prefix=KEY_PREFIX + "/", MaxKeys=int(limit))
    except Exception as e:
        raise StoreError(_safe_error(cfg, e)) from None
    return [{"key": item.get("Key"),
             "size_bytes": item.get("Size"),
             "last_modified": item.get("LastModified")}
            for item in (resp.get("Contents") or []) if item.get("Key")]
