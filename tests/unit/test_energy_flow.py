"""Reconstructing the arcs between solar, battery, grid and home.

The aGate reports four signed scalars per sample and no directional flows, so
a Sankey's edges are derived from a merit order. These tests pin that order
down, and pin down what happens when the data is bad — a missing reading, a
counter-less gap, a sample that cannot balance.
"""

from __future__ import annotations

import pytest

from franklinwh_bridge.gateway.energy_flow import (
    FLOWS,
    integrate,
    node_totals,
    split_sample,
)


def s(ts=0.0, solar=0.0, grid=0.0, battery=0.0, home=0.0):
    """grid > 0 imports, battery > 0 discharges — the bridge-wide convention."""
    return {"ts": ts, "solar_w": solar, "grid_w": grid,
            "battery_w": battery, "home_w": home}


# ── The merit order ───────────────────────────────────────────


def test_solar_serves_the_house_before_anything_else():
    f = split_sample(s(solar=1000, home=400, battery=-600))

    assert f["solar_to_home"] == 400
    assert f["solar_to_battery"] == 600
    assert f["solar_to_grid"] == 0


def test_surplus_solar_charges_before_it_exports():
    """2 kW spare with a 1.2 kW charge: the battery takes its fill, the grid
    gets the rest. Exporting in preference to charging would be wrong."""
    f = split_sample(s(solar=3000, home=1000, battery=-1200, grid=-800))

    assert f["solar_to_home"] == 1000
    assert f["solar_to_battery"] == 1200
    assert f["solar_to_grid"] == 800


def test_the_house_draws_the_battery_before_the_grid():
    f = split_sample(s(solar=0, home=1000, battery=600, grid=400))

    assert f["battery_to_home"] == 600
    assert f["grid_to_home"] == 400
    assert f["solar_to_home"] == 0


def test_night_time_import_is_all_grid_to_home():
    f = split_sample(s(solar=0, home=800, grid=800))

    assert f["grid_to_home"] == 800
    assert sum(v for k, v in f.items() if k != "grid_to_home") == 0


# ── The cases a single flag would get wrong ───────────────────


def test_battery_export_is_distinguished_from_solar_export():
    """A forced discharge to grid at night. Attributing this to solar would
    credit a feed-in tariff to generation that did not happen — and it is
    exactly what a battery-export ban is written about."""
    f = split_sample(s(solar=0, home=200, battery=2200, grid=-2000))

    assert f["battery_to_grid"] == 2000
    assert f["battery_to_home"] == 200
    assert f["solar_to_grid"] == 0


def test_grid_charging_is_distinguished_from_solar_charging():
    """Charging off-peak from the grid: 3 kW in, 500 W of it the house's."""
    f = split_sample(s(solar=0, home=500, battery=-2500, grid=3000))

    assert f["grid_to_battery"] == 2500
    assert f["grid_to_home"] == 500
    assert f["solar_to_battery"] == 0


def test_solar_and_grid_can_charge_together():
    """Topping up from the grid while the sun is also charging."""
    f = split_sample(s(solar=1000, home=400, battery=-2000, grid=1400))

    assert f["solar_to_home"] == 400
    assert f["solar_to_battery"] == 600
    assert f["grid_to_battery"] == 1400


# ── Bad data ──────────────────────────────────────────────────


def test_a_missing_reading_is_zero_flow_not_a_crash():
    """The poller returns None for a point it failed to read. One bad sample
    must not void the day."""
    f = split_sample({"solar_w": None, "grid_w": None,
                      "battery_w": None, "home_w": None})

    assert set(f) == set(FLOWS)
    assert sum(f.values()) == 0


def test_no_flow_is_ever_negative():
    """An arc is a direction; a negative one is meaningless. Inconsistent
    readings must clamp rather than produce a backwards arc."""
    f = split_sample(s(solar=100, home=5000, battery=-3000, grid=-2000))

    assert all(v >= 0 for v in f.values()), f


# ── Integration over time ─────────────────────────────────────


def test_power_is_held_until_the_next_sample():
    """1 kW for exactly one hour is 1 kWh."""
    flows = integrate([s(ts=0, solar=1000, home=1000), s(ts=3600)])

    assert flows["solar_to_home"] == pytest.approx(1.0)


def test_the_last_sample_contributes_nothing():
    """It has no interval yet — its energy lands in the next query."""
    assert integrate([s(ts=0, solar=1000, home=1000)])["solar_to_home"] == 0.0


def test_samples_out_of_order_are_sorted_not_trusted():
    a = integrate([s(ts=3600), s(ts=0, solar=1000, home=1000)])
    b = integrate([s(ts=0, solar=1000, home=1000), s(ts=3600)])

    assert a["solar_to_home"] == b["solar_to_home"] == pytest.approx(1.0)


def test_an_outage_is_capped_not_extrapolated():
    """The bridge stops polling while the container is down. Holding the last
    sample across an 8-hour gap would invent 8 kWh that never flowed; capped,
    the day under-reports visibly instead."""
    flows = integrate([s(ts=0, solar=1000, home=1000), s(ts=8 * 3600)],
                      max_gap_s=900)

    assert flows["solar_to_home"] == pytest.approx(0.25)
    assert flows["covered_s"] == 900


def test_wide_buckets_are_not_mistaken_for_an_outage():
    """A 90-day query auto-buckets to multi-hour steps. A fixed cap tight
    enough to catch an outage in today's seconds-apart data would truncate
    every one of those buckets and under-report the range by an order of
    magnitude, so the cap has to scale with the series."""
    hourly = [s(ts=h * 3600, solar=1000, home=1000) for h in range(25)]

    flows = integrate(hourly)

    assert flows["solar_to_home"] == pytest.approx(24.0)
    assert flows["covered_s"] == 24 * 3600


def test_an_outage_inside_dense_data_is_still_capped():
    """The same series, minus four hours in the middle: the surrounding steps
    set the scale, so the gap is recognised as anomalous."""
    dense = [s(ts=m * 60, solar=1000, home=1000) for m in range(60)]
    dense.append(s(ts=60 * 60 + 4 * 3600, solar=1000, home=1000))

    flows = integrate(dense)

    # 59 minutes of real data, plus a capped remnant of the 4-hour hole.
    assert flows["solar_to_home"] < 1.2
    assert flows["covered_s"] < 3600 + 600


def test_covered_time_reports_how_much_of_the_span_had_data():
    flows = integrate([s(ts=0), s(ts=600), s(ts=1200)])

    assert flows["covered_s"] == 1200
    assert flows["samples"] == 3


def test_an_empty_series_is_all_zero_not_an_error():
    flows = integrate([])

    assert flows["samples"] == 0
    assert all(flows[k] == 0.0 for k in FLOWS)


def test_an_unbalanced_sample_is_reported_as_residual():
    """Home draws 1 kW with nothing supplying it. The arcs cannot show that,
    so it must surface as a number rather than vanish."""
    flows = integrate([s(ts=0, home=1000), s(ts=3600)])

    assert flows["residual_kwh"] == pytest.approx(1.0)


def test_a_balanced_day_leaves_no_residual():
    flows = integrate([s(ts=0, solar=2000, home=1000, battery=-600, grid=-400),
                       s(ts=3600)])

    assert flows["residual_kwh"] == 0.0


# ── Node roll-up ──────────────────────────────────────────────


def test_node_totals_reconcile_with_the_arcs():
    flows = integrate([s(ts=0, solar=3000, home=1000, battery=-1200, grid=-800),
                       s(ts=3600)])
    n = node_totals(flows)

    assert n["solar"] == pytest.approx(3.0)
    assert n["home"] == pytest.approx(1.0)
    assert n["grid_export"] == pytest.approx(0.8)
    assert n["battery_charge"] == pytest.approx(1.2)
    assert n["grid_import"] == 0.0


def test_node_totals_tolerate_a_partial_flow_dict():
    """The API may hand back a trimmed dict; absent arcs are zero."""
    n = node_totals({"solar_to_home": 2.0})

    assert n["solar"] == 2.0
    assert n["home"] == 2.0
    assert n["grid_import"] == 0.0
