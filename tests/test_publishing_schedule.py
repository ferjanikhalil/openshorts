"""Pure-logic tests for publishing schedule/capacity planning and platform limits.

Like tests/test_publishing_state.py these import only stdlib-backed modules, so
they run in CI with no Postgres, no credentials and no network. `schedule` takes
`now` as a parameter precisely so this file can pin the clock.
"""
from datetime import datetime, timezone

import pytest

from publishing import platforms as plat
from publishing import schedule


def at(hour, minute=0, day=10):
    """A fixed instant in August 2026, UTC."""
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


class TestSpread:
    def test_empty(self):
        assert schedule.spread(0, now=at(10)) == []

    def test_first_slot_is_none_when_asap(self):
        # None, not `now`: a null scheduled_for is what makes the first attempt
        # immediately claimable instead of waiting for a clock to catch up.
        out = schedule.spread(3, now=at(10), spacing_seconds=600)
        assert out[0] is None
        assert out[1] == at(10, 10)
        assert out[2] == at(10, 20)

    def test_explicit_start_is_honoured(self):
        out = schedule.spread(2, now=at(10), start_at=at(14), spacing_seconds=900)
        assert out == [at(14), at(14, 15)]

    def test_spacing_is_applied_between_every_slot(self):
        out = schedule.spread(4, now=at(9), spacing_seconds=1800)
        stamps = [t for t in out if t is not None]
        gaps = {(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])}
        assert gaps == {1800}

    def test_early_morning_is_pushed_into_the_window(self):
        # 04:00 would waste a daily slot on the platform's worst hour.
        out = schedule.spread(1, now=at(4), start_at=at(4))
        assert out[0] == at(schedule.DEFAULT_WINDOW_START_HOUR)

    def test_past_the_window_rolls_to_tomorrow(self):
        out = schedule.spread(1, now=at(23), start_at=at(23))
        assert out[0] == at(schedule.DEFAULT_WINDOW_START_HOUR, day=11)

    def test_overflow_past_the_end_hour_rolls_over(self):
        # Enough posts at 21:00 to run past 22:00: the remainder must land in the
        # next day's window, not at 23:00 and 00:00.
        out = schedule.spread(4, now=at(21), start_at=at(21), spacing_seconds=1800)
        assert out[0] == at(21) and out[1] == at(21, 30)
        assert out[2] == at(schedule.DEFAULT_WINDOW_START_HOUR, day=11)
        assert out[3] == at(schedule.DEFAULT_WINDOW_START_HOUR, 30, day=11)

    def test_immediate_ignores_the_window(self):
        # `immediate` is the operator saying "now, I know what I'm doing".
        out = schedule.spread(2, now=at(3), spacing_seconds=60,
                              respect_window=False)
        assert out[0] is None
        assert out[1] == at(3, 1)

    def test_naive_start_is_treated_as_utc(self):
        out = schedule.spread(1, now=at(10),
                              start_at=datetime(2026, 8, 10, 14),
                              respect_window=False)
        assert out[0] == at(14)

    def test_zero_spacing_still_advances(self):
        # A 0 or negative spacing must not stack every post on one instant.
        out = schedule.spread(3, now=at(10), spacing_seconds=0,
                              respect_window=False)
        stamps = [t for t in out if t is not None]
        assert len(set(stamps)) == len(stamps)


class TestAllocate:
    def test_unknown_capacity_schedules_everything(self):
        # The provider only reports quota on a response, so before the day's
        # first post there is nothing to go on. Deferring costs a delay;
        # refusing costs a publication.
        fit, over = schedule.allocate(5, capacity=None)
        assert fit == [0, 1, 2, 3, 4] and over == []

    def test_splits_at_the_cap(self):
        fit, over = schedule.allocate(5, capacity=3)
        assert fit == [0, 1, 2] and over == [3, 4]

    def test_zero_capacity_defers_all(self):
        fit, over = schedule.allocate(2, capacity=0)
        assert fit == [] and over == [0, 1]

    def test_negative_capacity_is_treated_as_zero(self):
        fit, over = schedule.allocate(2, capacity=-5)
        assert fit == [] and over == [0, 1]

    def test_capacity_above_demand_is_not_padded(self):
        fit, over = schedule.allocate(2, capacity=99)
        assert fit == [0, 1] and over == []


class TestNextFreeSlot:
    def test_empty_queue_is_now(self):
        assert schedule.next_free_slot([], now=at(10)) is None

    def test_keeps_spacing_from_the_last_booking(self):
        got = schedule.next_free_slot([at(10), at(10, 30)], now=at(10),
                                      spacing_seconds=900)
        assert got == at(10, 45)

    def test_ignores_unscheduled_entries(self):
        got = schedule.next_free_slot([None, at(11)], now=at(10),
                                      spacing_seconds=600)
        assert got == at(11, 10)

    def test_stale_queue_returns_now(self):
        # Everything booked is already in the past — the slot is now, and `now`
        # is expressed as None for the same reason as in spread().
        assert schedule.next_free_slot([at(8)], now=at(12)) is None

    def test_unordered_input(self):
        got = schedule.next_free_slot([at(11), at(9)], now=at(8),
                                      spacing_seconds=600)
        assert got == at(11, 10)


class TestClipSelection:
    def test_no_default_cap(self):
        # Nothing here may impose a daily volume: that is the operator's
        # configuration, and a constant would silently become the ceiling.
        assert schedule.clip_selection(12) == list(range(12))

    def test_max_clips_truncates(self):
        assert schedule.clip_selection(12, max_clips=3) == [0, 1, 2]

    def test_explicit_indexes_win_over_max(self):
        assert schedule.clip_selection(12, clip_indexes=[5, 7],
                                       max_clips=1) == [5, 7]

    def test_explicit_indexes_are_deduped_and_ordered_as_given(self):
        assert schedule.clip_selection(12, clip_indexes=[7, 5, 7]) == [7, 5]

    def test_out_of_range_indexes_are_dropped(self):
        assert schedule.clip_selection(3, clip_indexes=[0, 99, -1]) == [0]

    def test_empty_job(self):
        assert schedule.clip_selection(0) == []


class TestPlatformLimits:
    def test_aliases_normalize(self):
        for alias in ("YT", "youtube_shorts", "shorts"):
            assert plat.normalize(alias) == plat.YOUTUBE
        for alias in ("ig", "reels", "instagram_reels"):
            assert plat.normalize(alias) == plat.INSTAGRAM
        assert plat.normalize("tt") == plat.TIKTOK

    def test_unknown_platform_passes_through(self):
        # The provider is the authority on what it can reach, so an unknown
        # platform is forwarded rather than rejected locally.
        assert plat.normalize("mastodon") == "mastodon"
        assert plat.max_video_bytes("mastodon") is None
        assert plat.check_video("mastodon", size_bytes=10 ** 12) is None

    def test_instagram_is_the_binding_video_limit(self):
        # The usual YouTube+Instagram+TikTok fan-out uploads one file, so the
        # smallest ceiling governs the set.
        got = plat.binding_video_limit(
            [plat.YOUTUBE, plat.INSTAGRAM, plat.TIKTOK])
        assert got == plat.max_video_bytes(plat.INSTAGRAM)

    def test_tiktok_is_the_binding_duration(self):
        # Different platform from the size ceiling: TikTok's 10 min is shorter
        # than Instagram's 15, while Instagram's 1 GB is smaller than TikTok's 4.
        got = plat.binding_duration_limit(
            [plat.YOUTUBE, plat.INSTAGRAM, plat.TIKTOK])
        assert got == plat.max_duration_seconds(plat.TIKTOK)

    def test_unknown_platforms_contribute_no_limit(self):
        assert plat.binding_video_limit(["mastodon"]) is None
        assert plat.binding_video_limit([]) is None

    def test_oversize_clip_is_reported_before_the_provider_sees_it(self):
        reason = plat.check_video(plat.INSTAGRAM, size_bytes=2 * 1024 ** 3)
        assert reason and "over the" in reason

    def test_overlong_clip_is_reported(self):
        reason = plat.check_video(plat.INSTAGRAM, duration_seconds=20 * 60)
        assert reason and "over the" in reason

    def test_a_normal_short_passes(self):
        assert plat.check_video(plat.INSTAGRAM, size_bytes=30 * 1024 * 1024,
                                duration_seconds=45) is None

    @pytest.mark.parametrize("platform,limit", [
        (plat.YOUTUBE, 5000), (plat.INSTAGRAM, 2200), (plat.TIKTOK, 2200)])
    def test_caption_limits(self, platform, limit):
        assert plat.caption_limit(platform) == limit
        assert plat.check_caption(platform, "x" * limit) is None
        assert plat.check_caption(platform, "x" * (limit + 1))

    def test_missing_caption_is_not_an_error(self):
        assert plat.check_caption(plat.TIKTOK, None) is None
        assert plat.check_caption(plat.TIKTOK, "") is None
