"""When a post should go out. Pure functions, no I/O — testable in CI.

Two problems are solved here, and they are not the same problem.

**Spacing.** The provider enforces a cooldown between posts to the same account
and a daily cap per account per platform. Firing a batch's clips at once means
the first succeeds and the rest come back 429, then walk a backoff for hours.
Spreading them at creation time replaces that with a plan: the queue is doing
what was asked instead of recovering from what it was asked.

**Capacity.** With a per-account daily cap, a day has a finite number of slots.
``allocate`` reports which clips fit and which do not, so the caller can say "3
of 5 clips scheduled, 2 need tomorrow" instead of silently enqueueing work that
will defer until the cap resets.

Nothing here reads the clock: ``now`` is always a parameter. That keeps the
functions deterministic under test, and it is the same reason the rest of the
package passes ``_now()`` in rather than calling it deep in a helper.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

# Default gap between two posts to the same account. Comfortably above the
# provider's documented spacing cooldown: the cost of being early is a 429 and a
# backoff, the cost of being late is a few extra minutes.
DEFAULT_SPACING_SECONDS = 15 * 60

# A day's posting window, in hours (local to whatever tz `now` carries). Posts
# are not spread across the small hours — a schedule that publishes at 04:00
# wastes a daily slot on the platform's worst engagement window.
DEFAULT_WINDOW_START_HOUR = 9
DEFAULT_WINDOW_END_HOUR = 22


def _floor_to_window(when: datetime, start_hour: int, end_hour: int) -> datetime:
    """Move ``when`` forward to the next moment inside the posting window."""
    if when.hour < start_hour:
        return when.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if when.hour >= end_hour:
        nxt = when + timedelta(days=1)
        return nxt.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    return when


def spread(count: int, *, now: datetime,
           spacing_seconds: int = DEFAULT_SPACING_SECONDS,
           start_at: Optional[datetime] = None,
           window_start_hour: int = DEFAULT_WINDOW_START_HOUR,
           window_end_hour: int = DEFAULT_WINDOW_END_HOUR,
           respect_window: bool = True) -> List[Optional[datetime]]:
    """Times for ``count`` posts, spaced and kept inside the posting window.

    The first element is ``None`` rather than ``now`` when no explicit start was
    given: "as soon as possible" is a different instruction from "at this exact
    timestamp", and a null ``scheduled_for`` is what makes the first attempt
    immediately claimable instead of waiting for a clock to catch up.
    """
    if count <= 0:
        return []
    out: List[Optional[datetime]] = []
    cursor = start_at or now
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)

    for i in range(count):
        if respect_window:
            cursor = _floor_to_window(cursor, window_start_hour, window_end_hour)
        if i == 0 and start_at is None:
            out.append(None if not respect_window or cursor <= now else cursor)
        else:
            out.append(cursor)
        cursor = cursor + timedelta(seconds=max(1, spacing_seconds))
    return out


def allocate(clip_count: int, *, capacity: Optional[int]) -> tuple:
    """Split clips into (schedulable, overflow) against a day's free slots.

    ``capacity`` of None means "unknown" — the provider only reports quota on a
    response, so before the first post of the day there is nothing to go on. An
    unknown capacity schedules everything and lets the dispatcher's quota gate
    defer what does not fit, which is the safe direction: deferring a post costs
    a delay, refusing one costs a publication.
    """
    idx = list(range(max(0, clip_count)))
    if capacity is None:
        return idx, []
    cap = max(0, int(capacity))
    return idx[:cap], idx[cap:]


def next_free_slot(existing: Sequence[Optional[datetime]], *, now: datetime,
                   spacing_seconds: int = DEFAULT_SPACING_SECONDS
                   ) -> Optional[datetime]:
    """Earliest time that keeps ``spacing_seconds`` from everything already booked.

    Used when a post is added to an account that already has a queue, e.g. a
    manual publish while an autopilot batch's schedule is still draining.
    Returns None when the slot is now — see ``spread`` for why that is not
    ``now``.
    """
    booked = sorted(t for t in existing if t is not None)
    if not booked:
        return None
    last = booked[-1]
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    candidate = last + timedelta(seconds=max(1, spacing_seconds))
    return candidate if candidate > now else None


def clip_selection(clip_count: int, *, clip_indexes=None,
                   max_clips: Optional[int] = None) -> List[int]:
    """Which clips of a finished job to publish.

    Explicit indexes win; otherwise the first ``max_clips``. No default cap:
    the operator's daily volume is a configuration choice, and a number baked in
    here would silently become the system's ceiling.
    """
    if clip_indexes:
        return [i for i in dict.fromkeys(int(i) for i in clip_indexes)
                if 0 <= i < clip_count]
    idx = list(range(max(0, clip_count)))
    if max_clips:
        idx = idx[:max(0, int(max_clips))]
    return idx
