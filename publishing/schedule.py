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


# --- Posting rhythm (per-group plan) -----------------------------------------
# A rhythm is the operator's instruction "this batch starts at 06:00 and posts
# one clip every N hours, at most M a day" — the schedule equivalent of a
# recipe. It lives in ``publish_groups.settings['plan']`` and every function
# here is pure: the same plan, ``now`` and bookings always produce the same
# slots, which is what makes a preview honest and a test deterministic.

RHYTHM_MODE = "rhythm"
DEFAULT_RHYTHM_START = "06:00"
DEFAULT_RHYTHM_INTERVAL_HOURS = 6.0
# 3/day against the free tier's 5/day leaves headroom of two for retries and
# manual posts — see the quota note in providers/status200.py.
DEFAULT_RHYTHM_MAX_PER_DAY = 3
RHYTHM_CATCHUP_POLICIES = ("next_slot", "skip", "immediate")
# How far a slot may pass before the catch-up policy engages. Generous on
# purpose: a dispatch a few minutes late (retry backoff, worker restart) is the
# rhythm working, not the rhythm broken.
CATCH_UP_GRACE_SECONDS = 15 * 60


def normalize_plan(raw: Optional[dict]) -> Optional[dict]:
    """Coerce a stored plan into canonical shape, or None when not a rhythm.

    Raises ValueError with an operator-readable message on bad input — the
    admin API turns it into a 400 — so a typo cannot silently fall back to
    defaults the operator never chose.
    """
    raw = raw or {}
    if raw.get("mode") != RHYTHM_MODE:
        return None
    plan = dict(raw)
    plan["mode"] = RHYTHM_MODE

    start = str(raw.get("start_time") or DEFAULT_RHYTHM_START).strip()
    parts = start.split(":")
    try:
        h, m = int(parts[0]), int(parts[1] if len(parts) > 1 else 0)
        assert 0 <= h < 24 and 0 <= m < 60
    except Exception:
        raise ValueError(f"start_time must be HH:MM, got {start!r}")
    plan["start_time"] = f"{h:02d}:{m:02d}"

    try:
        raw_interval = raw.get("interval_hours")
        interval = float(raw_interval if raw_interval is not None
                         else DEFAULT_RHYTHM_INTERVAL_HOURS)
        assert 0 < interval <= 24
    except Exception:
        raise ValueError("interval_hours must be a number between 0 and 24")
    plan["interval_hours"] = interval

    try:
        raw_cap = raw.get("max_per_day")
        cap = int(raw_cap if raw_cap is not None
                  else DEFAULT_RHYTHM_MAX_PER_DAY)
        assert 1 <= cap <= 24
    except Exception:
        raise ValueError("max_per_day must be an integer between 1 and 24")
    plan["max_per_day"] = cap

    plan["timezone"] = _valid_timezone(str(raw.get("timezone") or "UTC"))

    catch_up = str(raw.get("catch_up") or "next_slot")
    if catch_up not in RHYTHM_CATCHUP_POLICIES:
        raise ValueError(f"catch_up must be one of {RHYTHM_CATCHUP_POLICIES}")
    plan["catch_up"] = catch_up
    return plan


def _valid_timezone(name: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ValueError(f"unknown timezone {name!r}")


def stagger_for_group(group_id, max_minutes: int = 15) -> int:
    """Deterministic minute offset so several groups on the same start time do
    not all submit in the same second.

    Hashed, not random, so a slot preview does not change between renders, and
    not the group's position in some list, so reordering groups in the UI does
    not silently move everyone's schedule.
    """
    import hashlib
    digest = hashlib.sha256(str(group_id).encode()).hexdigest()
    return int(digest[:6], 16) % max(1, max_minutes)


def rhythm_slots(plan: dict, count: int, *, now: datetime,
                 booked: Sequence[datetime] = (),
                 daily_cap: Optional[int] = None,
                 group_id: str = "") -> dict:
    """Times for ``count`` posts on a group's rhythm.

    Returns ``{"slots": [...], "per_day": {iso-date: n}}``. The grid starts at
    ``start_time`` and steps by ``interval_hours``; a calendar day (in the
    plan's timezone) never carries more than ``min(max_per_day, daily_cap)``
    slots INCLUDING already-booked ones, so a second autopilot run lands on
    free capacity instead of colliding with the first.

    ``now`` is a parameter like everywhere else in this module: the caller owns
    the clock, tests stay deterministic.
    """
    plan = normalize_plan(plan) or {"start_time": DEFAULT_RHYTHM_START,
                                    "interval_hours":
                                        DEFAULT_RHYTHM_INTERVAL_HOURS,
                                    "max_per_day": DEFAULT_RHYTHM_MAX_PER_DAY,
                                    "timezone": "UTC"}
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(plan["timezone"])
    cap = min(plan["max_per_day"],
              daily_cap if daily_cap is not None else plan["max_per_day"])

    start_h, start_m = (int(x) for x in plan["start_time"].split(":"))
    interval = timedelta(hours=plan["interval_hours"])
    stagger = timedelta(minutes=stagger_for_group(group_id))

    # Bookings counted against each day's cap, in the plan's tz.
    used: dict = {}
    for t in booked:
        if t is None:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        day = t.astimezone(tz).date().isoformat()
        used[day] = used.get(day, 0) + 1

    local_now = now.astimezone(tz) if now.tzinfo else now.replace(
        tzinfo=timezone.utc).astimezone(tz)
    # Candidate slots are built in naive local time and localized only on
    # output: arithmetic stays simple, and DST folds are resolved once at the
    # edge rather than on every comparison.
    naive_now = local_now.replace(tzinfo=None)
    from datetime import datetime as _dt

    def day_start(d):
        return _dt(d.year, d.month, d.day, start_h, start_m) + stagger

    def step(c):
        """Advance one interval; a step that crosses midnight restarts the next
        day at ``start_time`` rather than emitting a small-hours slot. The
        rhythm is "every N hours from the start time", not "every N hours
        forever" — 06:00/12:00/18:00 must be followed by tomorrow 06:00, never
        by midnight."""
        nxt = c + interval
        if nxt.date() != c.date():
            nxt = day_start(nxt.date())
        return nxt

    slots: List[datetime] = []
    per_day: dict = {}
    candidate = day_start(naive_now.date())
    while candidate <= naive_now:
        candidate = step(candidate)

    guard = 0
    while len(slots) < count and guard < 400:
        guard += 1
        day_key = candidate.date().isoformat()
        day_cap_left = cap - used.get(day_key, 0)
        if day_cap_left <= 0:
            # Tomorrow's first slot; a new day resets the cap.
            candidate = day_start(candidate.date() + timedelta(days=1))
            if candidate <= naive_now:
                candidate = step(candidate)
            continue
        slots.append(candidate.replace(tzinfo=tz).astimezone(timezone.utc))
        per_day[day_key] = per_day.get(day_key, 0) + 1
        used[day_key] = used.get(day_key, 0) + 1
        candidate = step(candidate)
    return {"slots": slots, "per_day": per_day}
