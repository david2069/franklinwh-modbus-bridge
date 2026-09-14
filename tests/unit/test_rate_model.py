"""Seasonal TOU rate resolution.

Built against a real bill (AGL Residential TOU, 2026-09) rather than an
invented plan, because the awkward part is real: a plan's import and export
boundaries need not line up.
"""

from __future__ import annotations

import datetime as dt

import pytest

from franklinwh_bridge.gateway.rate_model import WAVES, find_season, resolve, validate

PEAK_WAVES = {
    "super_off_peak": {"buy": 0.21626, "sell": 0.03},
    "off_peak": {"buy": 0.21626, "sell": 0.03},
    "mid_peak": {"buy": 0.54175, "sell": 0.28},
    "on_peak": {"buy": 0.54175, "sell": 0.03},
}
PEAK_BLOCKS = [
    {"start": "00:00", "end": "07:00", "wave": "off_peak"},
    {"start": "07:00", "end": "08:00", "wave": "super_off_peak"},
    {"start": "08:00", "end": "15:00", "wave": "off_peak"},
    {"start": "15:00", "end": "17:00", "wave": "on_peak"},
    {"start": "17:00", "end": "21:00", "wave": "mid_peak"},
    {"start": "21:00", "end": "24:00", "wave": "off_peak"},
]
AGL = [
    {"id": "peak", "name": "Peak season", "months": [11, 12, 1, 2, 3, 6, 7, 8],
     "waves": PEAK_WAVES, "blocks": PEAK_BLOCKS},
    {"id": "shoulder", "name": "Shoulder season", "months": [4, 5, 9, 10],
     "waves": {**PEAK_WAVES,
               "mid_peak": {"buy": 0.21626, "sell": 0.28},
               "on_peak": {"buy": 0.0, "sell": 0.0}},
     "blocks": [
         {"start": "00:00", "end": "07:00", "wave": "off_peak"},
         {"start": "07:00", "end": "08:00", "wave": "super_off_peak"},
         {"start": "08:00", "end": "17:00", "wave": "off_peak"},
         {"start": "17:00", "end": "21:00", "wave": "mid_peak"},
         {"start": "21:00", "end": "24:00", "wave": "off_peak"},
     ]},
]


def at(month: int, hour: int, day: int = 14) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, 0)


# ── The boundary mismatch this model exists for ───────────────


def test_import_peak_splits_where_the_feed_in_boundary_falls():
    """AGL's peak IMPORT is 15:00-21:00 but its evening FiT is 17:00-21:00.
    The span is two waves sharing a buy price and differing on sell — the case
    that separate import/export window structures cannot express."""
    early = resolve(AGL, at(1, 16))
    late = resolve(AGL, at(1, 18))

    assert early["buy"] == late["buy"] == 0.54175
    assert early["sell"] == 0.03
    assert late["sell"] == 0.28
    assert (early["wave"], late["wave"]) == ("on_peak", "mid_peak")


def test_base_feed_in_tariff_has_a_home():
    """The 3c off-peak FiT applies to most exported kWh and previously had
    nowhere to be entered at all."""
    assert resolve(AGL, at(1, 12))["sell"] == 0.03
    assert resolve(AGL, at(1, 2))["sell"] == 0.03


def test_shoulder_season_has_no_peak_import_but_keeps_the_evening_feed_in():
    """Apr/May/Sep/Oct: off-peak all day, yet the 28c evening FiT still runs."""
    afternoon = resolve(AGL, at(9, 16))
    evening = resolve(AGL, at(9, 18))

    assert afternoon["buy"] == 0.21626
    assert evening["buy"] == 0.21626, "no peak import in shoulder season"
    assert evening["sell"] == 0.28, "evening FiT applies year-round"


def test_a_wave_is_named_not_price_derived():
    """AGL's 07:00-08:00 morning band prices identically to off-peak and is
    still its own wave. Waves must never be deduped by price."""
    r = resolve(AGL, at(1, 7))

    assert r["wave"] == "super_off_peak"
    assert (r["buy"], r["sell"]) == (0.21626, 0.03)


# ── Season selection ──────────────────────────────────────────


@pytest.mark.parametrize("month,expected", [
    (1, "Peak season"), (7, "Peak season"), (12, "Peak season"),
    (4, "Shoulder season"), (9, "Shoulder season"), (10, "Shoulder season"),
])
def test_month_picks_the_season(month, expected):
    assert resolve(AGL, at(month, 12))["season"] == expected


def test_an_all_year_season_is_only_a_fallback():
    """Otherwise adding a catch-all would shadow every seasonal one."""
    seasons = [
        {"id": "all", "name": "All year", "months": [], "waves": PEAK_WAVES,
         "blocks": PEAK_BLOCKS},
        *AGL,
    ]
    assert find_season(seasons, at(1, 12))["id"] == "peak"
    assert find_season(seasons, at(5, 12))["id"] == "shoulder"


def test_unmatched_month_says_so():
    seasons = [{"id": "winter", "months": [6, 7], "waves": PEAK_WAVES, "blocks": PEAK_BLOCKS}]

    assert resolve(seasons, at(1, 12))["reason"] == "no_season_for_month"


# ── Blocks ────────────────────────────────────────────────────


def test_a_block_may_wrap_past_midnight():
    seasons = [{"id": "s", "months": [], "waves": PEAK_WAVES,
                "blocks": [{"start": "21:00", "end": "06:00", "wave": "off_peak"}]}]

    assert resolve(seasons, at(1, 23))["wave"] == "off_peak"
    assert resolve(seasons, at(1, 2))["wave"] == "off_peak"
    assert resolve(seasons, at(1, 12))["reason"] == "no_block_for_time"


def test_blocks_can_differ_by_day_of_week():
    """Weekday/weekend rates, without a separate structure."""
    seasons = [{"id": "s", "months": [], "waves": PEAK_WAVES, "blocks": [
        {"start": "00:00", "end": "24:00", "wave": "on_peak", "days": [0, 1, 2, 3, 4]},
        {"start": "00:00", "end": "24:00", "wave": "off_peak", "days": [5, 6]},
    ]}]

    # January 2026: the 5th is a Monday, the 10th a Saturday. (September's
    # weekday dates don't transfer — the first version of this test used them.)
    assert resolve(seasons, at(1, 12, day=5))["wave"] == "on_peak"     # Monday
    assert resolve(seasons, at(1, 12, day=10))["wave"] == "off_peak"   # Saturday


def test_an_uncovered_hour_is_reported_not_guessed():
    """A gap must not resolve to zero — that silently under-bills."""
    seasons = [{"id": "s", "months": [], "waves": PEAK_WAVES,
                "blocks": [{"start": "00:00", "end": "12:00", "wave": "off_peak"}]}]

    r = resolve(seasons, at(1, 18))

    assert r["reason"] == "no_block_for_time"
    assert r["buy"] is None and r["sell"] is None


# ── Billable / free import ────────────────────────────────────


def test_a_zero_buy_wave_is_a_free_import_window():
    """The AU "free grid import" plan shape is just a wave priced at zero —
    no special-casing."""
    free = {**PEAK_WAVES, "off_peak": {"buy": 0.0, "sell": 0.03}}
    seasons = [{"id": "s", "months": [], "waves": free,
                "blocks": [{"start": "11:00", "end": "13:00", "wave": "off_peak"},
                           {"start": "13:00", "end": "11:00", "wave": "on_peak"}]}]

    assert resolve(seasons, at(1, 12))["billable"] is False
    assert resolve(seasons, at(1, 20))["billable"] is True


def test_unconfigured_is_unknown_not_free():
    r = resolve(None, at(1, 12))

    assert r["billable"] is None
    assert r["reason"] == "not_configured"


# ── Validation ────────────────────────────────────────────────


def test_the_real_plan_validates_clean():
    assert validate(AGL) == []


def test_a_coverage_gap_is_reported():
    seasons = [{"id": "s", "name": "Partial", "months": [], "waves": PEAK_WAVES,
                "blocks": [{"start": "00:00", "end": "12:00", "wave": "off_peak"}]}]

    problems = validate(seasons)

    assert any("prices no rate" in p for p in problems)


def test_two_seasons_claiming_a_month_is_reported():
    seasons = [
        {"id": "a", "name": "A", "months": [1], "waves": PEAK_WAVES, "blocks": PEAK_BLOCKS},
        {"id": "b", "name": "B", "months": [1], "waves": PEAK_WAVES, "blocks": PEAK_BLOCKS},
    ]

    assert any("claimed by both" in p for p in validate(seasons))


def test_a_block_naming_an_unpriced_wave_is_reported():
    seasons = [{"id": "s", "name": "S", "months": [],
                "waves": {"off_peak": {"buy": 0.2, "sell": 0.03}},
                "blocks": [{"start": "00:00", "end": "24:00", "wave": "on_peak"}]}]

    assert any("prices no rate for it" in p for p in validate(seasons))


def test_wave_vocabulary_is_fixed():
    assert WAVES == ("super_off_peak", "off_peak", "mid_peak", "on_peak")
