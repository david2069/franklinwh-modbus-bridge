"""Monthly (calendar) + custom-cron trigger kinds — next/prev fire."""

from __future__ import annotations

from datetime import datetime

from franklinwh_bridge.gateway.scheduler_triggers import (
    VALID_KINDS,
    next_fire_at,
    prev_fire_at,
)

NOW = datetime(2026, 8, 13, 14, 30)  # a Thursday


def test_kinds_registered():
    assert "monthly" in VALID_KINDS
    assert "cron" in VALID_KINDS


# ── monthly ───────────────────────────────────────────────────
def test_monthly_every_month_first():
    spec = {"kind": "monthly", "day": 1, "time_of_day": "00:00"}
    assert next_fire_at(spec, NOW) == datetime(2026, 9, 1, 0, 0)
    assert prev_fire_at(spec, NOW) == datetime(2026, 8, 1, 0, 0)


def test_monthly_quarterly():
    spec = {"kind": "monthly", "day": 1, "months": [1, 4, 7, 10], "time_of_day": "00:00"}
    assert next_fire_at(spec, NOW) == datetime(2026, 10, 1, 0, 0)


def test_monthly_six_monthly_and_annual():
    six = {"kind": "monthly", "day": 1, "months": [1, 7]}
    assert next_fire_at(six, NOW) == datetime(2027, 1, 1, 0, 0)  # Jul passed → Jan
    annual = {"kind": "monthly", "day": 1, "months": [1]}
    assert next_fire_at(annual, NOW) == datetime(2027, 1, 1, 0, 0)


def test_monthly_day_clamps_to_month_length():
    # day 31 in February → clamps to the 28th (2027 not a leap year)
    spec = {"kind": "monthly", "day": 31, "months": [2], "time_of_day": "09:00"}
    assert next_fire_at(spec, NOW) == datetime(2027, 2, 28, 9, 0)


def test_monthly_same_day_later_today_fires_today():
    spec = {"kind": "monthly", "day": 13, "time_of_day": "18:00"}  # today 18:00 > 14:30
    assert next_fire_at(spec, NOW) == datetime(2026, 8, 13, 18, 0)


def test_monthly_invalid_day():
    assert next_fire_at({"kind": "monthly", "day": 40}, NOW) is None


# ── cron ──────────────────────────────────────────────────────
def test_cron_interval():
    assert next_fire_at({"kind": "cron", "expr": "*/5 * * * *"}, NOW) == NOW  # 14:30 matches


def test_cron_next_and_prev():
    spec = {"kind": "cron", "expr": "0 6 * * *"}  # 06:00 daily
    assert next_fire_at(spec, NOW) == datetime(2026, 8, 14, 6, 0)
    assert prev_fire_at(spec, NOW) == datetime(2026, 8, 13, 6, 0)


def test_cron_monthly_first():
    assert next_fire_at({"kind": "cron", "expr": "0 0 1 * *"}, NOW) == datetime(2026, 9, 1, 0, 0)


def test_cron_invalid_returns_none():
    assert next_fire_at({"kind": "cron", "expr": "nonsense"}, NOW) is None
    assert next_fire_at({"kind": "cron", "expr": ""}, NOW) is None
    assert prev_fire_at({"kind": "cron", "expr": "bad bad"}, NOW) is None
