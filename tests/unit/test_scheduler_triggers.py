"""Unit tests for scheduler v2 trigger evaluation (pure, no hardware)."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from franklinwh_bridge.gateway.scheduler_triggers import next_fire_at, prev_fire_at

# ── oneoff ────────────────────────────────────────────────────


def test_oneoff_future_returns_fire_at():
    now = datetime(2026, 8, 6, 8, 0)
    trig = {"kind": "oneoff", "fire_at": "2026-08-06T18:30:00"}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 18, 30)


def test_oneoff_exactly_now_fires():
    now = datetime(2026, 8, 6, 8, 0)
    trig = {"kind": "oneoff", "fire_at": "2026-08-06T08:00:00"}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 8, 0)


def test_oneoff_past_returns_none():
    now = datetime(2026, 8, 6, 8, 0)
    trig = {"kind": "oneoff", "fire_at": "2026-08-05T18:30:00"}
    assert next_fire_at(trig, now) is None


def test_oneoff_invalid_fire_at_returns_none():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at({"kind": "oneoff", "fire_at": "not-a-date"}, now) is None
    assert next_fire_at({"kind": "oneoff"}, now) is None


def test_oneoff_aware_fire_at_naive_now_reconciled():
    # aware fire_at + naive now must not raise; awareness is coerced to now's
    now = datetime(2026, 8, 6, 8, 0)
    trig = {"kind": "oneoff", "fire_at": "2026-08-06T18:30:00+10:00"}
    result = next_fire_at(trig, now)
    assert result == datetime(2026, 8, 6, 18, 30)  # tz dropped to match naive now


# ── daily ─────────────────────────────────────────────────────


def test_daily_later_today():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at({"kind": "daily", "time_of_day": "09:00"}, now) == datetime(
        2026, 8, 6, 9, 0
    )


def test_daily_time_passed_rolls_to_tomorrow():
    now = datetime(2026, 8, 6, 10, 0)
    assert next_fire_at({"kind": "daily", "time_of_day": "09:00"}, now) == datetime(
        2026, 8, 7, 9, 0
    )


def test_daily_exactly_now():
    now = datetime(2026, 8, 6, 9, 0, 30)  # seconds are zeroed internally
    assert next_fire_at({"kind": "daily", "time_of_day": "09:00"}, now) == datetime(
        2026, 8, 6, 9, 0
    )


def test_daily_invalid_time_returns_none():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at({"kind": "daily", "time_of_day": "25:00"}, now) is None
    assert next_fire_at({"kind": "daily"}, now) is None


# ── weekly ────────────────────────────────────────────────────


def test_weekly_same_day_later():
    now = datetime(2026, 8, 6, 8, 0)
    trig = {"kind": "weekly", "time_of_day": "12:00", "days_of_week": [now.weekday()]}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 12, 0)


def test_weekly_time_passed_rolls_a_week():
    now = datetime(2026, 8, 6, 14, 0)
    trig = {"kind": "weekly", "time_of_day": "12:00", "days_of_week": [now.weekday()]}
    assert next_fire_at(trig, now) == datetime(2026, 8, 13, 12, 0)


def test_weekly_picks_nearest_future_day():
    now = datetime(2026, 8, 6, 8, 0)
    target = (now.weekday() + 2) % 7
    trig = {"kind": "weekly", "time_of_day": "12:00", "days_of_week": [target]}
    result = next_fire_at(trig, now)
    assert result.weekday() == target
    assert result.time() == time(12, 0)
    assert 0 < (result.date() - now.date()).days <= 7


def test_weekly_empty_days_returns_none():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at({"kind": "weekly", "time_of_day": "12:00", "days_of_week": []}, now) is None


# ── interval ──────────────────────────────────────────────────


def test_interval_midnight_anchor_next_hour():
    now = datetime(2026, 8, 6, 8, 30)
    trig = {"kind": "interval", "every_seconds": 3600}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 9, 0)


def test_interval_exactly_on_boundary():
    now = datetime(2026, 8, 6, 9, 0)
    trig = {"kind": "interval", "every_seconds": 3600}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 9, 0)


def test_interval_explicit_anchor_time():
    now = datetime(2026, 8, 6, 8, 30)
    trig = {"kind": "interval", "every_seconds": 3600, "anchor_time": "06:00"}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 9, 0)


def test_interval_quarter_hour():
    now = datetime(2026, 8, 6, 8, 37)
    trig = {"kind": "interval", "every_seconds": 900}
    assert next_fire_at(trig, now) == datetime(2026, 8, 6, 8, 45)


@pytest.mark.parametrize("every", [0, -60, "nope", None])
def test_interval_invalid_every_returns_none(every):
    now = datetime(2026, 8, 6, 8, 30)
    assert next_fire_at({"kind": "interval", "every_seconds": every}, now) is None


# ── always / invalid ──────────────────────────────────────────


def test_always_has_no_discrete_fire():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at({"kind": "always"}, now) is None


def test_unknown_kind_returns_none():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at({"kind": "cron"}, now) is None


def test_non_dict_trigger_returns_none():
    now = datetime(2026, 8, 6, 8, 0)
    assert next_fire_at(None, now) is None
    assert next_fire_at("daily", now) is None


# ── timezone / DST awareness ──────────────────────────────────


def test_daily_timezone_aware_preserves_wall_time():
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 1, 0, tzinfo=ny)  # DST spring-forward day (02:00→03:00)
    result = next_fire_at({"kind": "daily", "time_of_day": "09:00"}, now)
    assert result is not None
    assert (result.year, result.month, result.day) == (2026, 3, 8)
    assert (result.hour, result.minute) == (9, 0)
    assert result.tzinfo is ny  # stays aware in the same zone


def test_aware_now_returns_aware_fire():
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 6, 8, 0, tzinfo=ny)
    result = next_fire_at({"kind": "daily", "time_of_day": "09:00"}, now)
    assert result.tzinfo is ny
    assert result >= now


# ── prev_fire_at (backward: most recent fire ≤ now) ───────────


def test_prev_oneoff_past_and_future():
    now = datetime(2026, 8, 6, 10, 0)
    assert prev_fire_at({"kind": "oneoff", "fire_at": "2026-08-06T09:00:00"}, now) == datetime(
        2026, 8, 6, 9, 0
    )
    # future fire has no "previous"
    assert prev_fire_at({"kind": "oneoff", "fire_at": "2026-08-06T11:00:00"}, now) is None


def test_prev_daily_today_vs_yesterday():
    assert prev_fire_at({"kind": "daily", "time_of_day": "09:00"}, datetime(2026, 8, 6, 10, 0)) == (
        datetime(2026, 8, 6, 9, 0)
    )
    # before today's time → yesterday's occurrence
    assert prev_fire_at({"kind": "daily", "time_of_day": "09:00"}, datetime(2026, 8, 6, 8, 0)) == (
        datetime(2026, 8, 5, 9, 0)
    )


def test_prev_daily_exactly_now():
    now = datetime(2026, 8, 6, 9, 0)
    assert prev_fire_at({"kind": "daily", "time_of_day": "09:00"}, now) == datetime(
        2026, 8, 6, 9, 0
    )


def test_prev_weekly_scans_back():
    now = datetime(2026, 8, 6, 8, 0)  # after 12:00? no — 08:00
    trig = {"kind": "weekly", "time_of_day": "12:00", "days_of_week": [now.weekday()]}
    # today's 12:00 hasn't happened yet at 08:00 → previous week same weekday
    result = prev_fire_at(trig, now)
    assert result.weekday() == now.weekday()
    assert result <= now
    assert 0 < (now.date() - result.date()).days <= 7


def test_prev_interval_current_window_start():
    now = datetime(2026, 8, 6, 8, 30)
    assert prev_fire_at({"kind": "interval", "every_seconds": 3600}, now) == datetime(
        2026, 8, 6, 8, 0
    )
    # exactly on a boundary returns that boundary
    assert prev_fire_at(
        {"kind": "interval", "every_seconds": 3600}, datetime(2026, 8, 6, 9, 0)
    ) == datetime(2026, 8, 6, 9, 0)


def test_prev_always_and_invalid_return_none():
    now = datetime(2026, 8, 6, 8, 0)
    assert prev_fire_at({"kind": "always"}, now) is None
    assert prev_fire_at({"kind": "cron"}, now) is None
    assert prev_fire_at(None, now) is None


# ── day_segments (timeline preview) ──────────────────────────


def test_day_segments_daily_every_weekday():
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments
    for wd in range(7):
        segs = day_segments("daily", {"time_of_day": "18:00"}, 7200, wd)
        assert segs == [(1080, 1080 + 120)]  # 18:00 for 120 min


def test_day_segments_weekly_only_matching_days():
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments
    spec = {"time_of_day": "09:00", "days_of_week": [0, 2]}  # Mon, Wed
    assert day_segments("weekly", spec, 1800, 0) == [(540, 570)]
    assert day_segments("weekly", spec, 1800, 1) == []  # Tue


def test_day_segments_min_width_for_zero_duration():
    from franklinwh_bridge.gateway.scheduler_triggers import MIN_SEGMENT_MIN, day_segments
    segs = day_segments("daily", {"time_of_day": "06:00"}, None, 0)
    assert segs == [(360, 360 + MIN_SEGMENT_MIN)]


def test_day_segments_interval_tiles_the_day():
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments
    segs = day_segments("interval", {"every_seconds": 3600}, 900, 0)  # hourly, 15 min
    assert len(segs) == 24
    assert segs[0] == (0, 15)
    assert segs[1] == (60, 75)


def test_day_segments_clamps_past_midnight():
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments
    segs = day_segments("daily", {"time_of_day": "23:30"}, 7200, 0)  # 23:30 + 2h
    assert segs == [(1410, 1440)]  # clamped to end-of-day


def test_day_segments_oneoff_on_matching_weekday_only():
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments
    # 2026-06-15 is a Monday (weekday 0)
    spec = {"fire_at": "2026-06-15T14:00:00"}
    assert day_segments("oneoff", spec, 3600, 0) == [(840, 900)]
    assert day_segments("oneoff", spec, 3600, 1) == []


def test_day_segments_always_is_empty():
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments
    assert day_segments("always", {}, 3600, 0) == []
