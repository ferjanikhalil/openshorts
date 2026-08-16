"""Platform identities and media ceilings. Pure data + pure functions.

These are properties of the SOCIAL PLATFORM, not of the provider, so they live
outside ``providers/``: Instagram's 1 GB video ceiling applies no matter which
API puts the video there.

The numbers are the published platform limits as of the last verification
(2026-08). They are a pre-flight guard, not the authority — the provider still
rejects an oversize upload, and that rejection is classified
``media_too_large``. The point of checking locally is to fail before burning a
daily quota slot on a post that cannot succeed.
"""
from typing import Iterable, Optional

YOUTUBE = "youtube"
INSTAGRAM = "instagram"
TIKTOK = "tiktok"

# Platforms the application understands. A provider may support more; an unknown
# platform is passed through rather than rejected, since the provider is the
# authority on what it can reach.
KNOWN_PLATFORMS = (YOUTUBE, INSTAGRAM, TIKTOK)

_MB = 1024 * 1024
_GB = 1024 * _MB

# platform -> (max_image_bytes, max_video_bytes, max_duration_seconds)
LIMITS = {
    YOUTUBE:   (2 * _MB,   256 * _GB, 12 * 3600),
    INSTAGRAM: (8 * _MB,   1 * _GB,   15 * 60),
    TIKTOK:    (20 * _MB,  4 * _GB,   10 * 60),
}

# Caption/title ceilings. Truncation is the caller's decision, not ours — this
# module only reports.
CAPTION_LIMITS = {
    YOUTUBE: 5000,
    INSTAGRAM: 2200,
    TIKTOK: 2200,
}
TITLE_LIMITS = {
    YOUTUBE: 100,
}


def normalize(platform: str) -> str:
    p = (platform or "").strip().lower()
    aliases = {
        "yt": YOUTUBE, "youtube_shorts": YOUTUBE, "shorts": YOUTUBE,
        "ig": INSTAGRAM, "reels": INSTAGRAM, "instagram_reels": INSTAGRAM,
        "tt": TIKTOK,
    }
    return aliases.get(p, p)


def max_video_bytes(platform: str) -> Optional[int]:
    lim = LIMITS.get(normalize(platform))
    return lim[1] if lim else None


def max_duration_seconds(platform: str) -> Optional[int]:
    lim = LIMITS.get(normalize(platform))
    return lim[2] if lim else None


def caption_limit(platform: str) -> Optional[int]:
    return CAPTION_LIMITS.get(normalize(platform))


def binding_video_limit(platforms: Iterable[str]) -> Optional[int]:
    """Tightest video ceiling across a destination set.

    One clip fans out to several platforms from a single uploaded file, so the
    smallest ceiling governs the whole set — Instagram's 1 GB, in the usual
    YouTube+Instagram+TikTok fan-out. Unknown platforms contribute no limit
    rather than an infinite one.
    """
    caps = [max_video_bytes(p) for p in platforms]
    caps = [c for c in caps if c]
    return min(caps) if caps else None


def binding_duration_limit(platforms: Iterable[str]) -> Optional[int]:
    caps = [max_duration_seconds(p) for p in platforms]
    caps = [c for c in caps if c]
    return min(caps) if caps else None


def check_video(platform: str, size_bytes: Optional[int] = None,
                duration_seconds: Optional[float] = None) -> Optional[str]:
    """Return a human-readable reason the clip cannot post, or None if it can."""
    p = normalize(platform)
    lim = LIMITS.get(p)
    if not lim:
        return None
    _img, max_bytes, max_dur = lim
    if size_bytes and size_bytes > max_bytes:
        return (f"{p}: file is {size_bytes / _MB:.1f} MB, over the "
                f"{max_bytes / _MB:.0f} MB limit")
    if duration_seconds and duration_seconds > max_dur:
        return (f"{p}: clip is {duration_seconds:.0f}s, over the "
                f"{max_dur}s limit")
    return None


def check_caption(platform: str, caption: Optional[str]) -> Optional[str]:
    limit = caption_limit(platform)
    if not limit or not caption:
        return None
    if len(caption) > limit:
        return (f"{normalize(platform)}: caption is {len(caption)} chars, over "
                f"the {limit} limit")
    return None
