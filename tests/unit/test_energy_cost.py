"""Consumption cost, integrated against the rate in force.

Until the rate model landed, a billing period's net_total was surcharges only —
demand, the two-way export charge, standing charges, less the export bonus. The
kWh actually bought and sold, which is most of a real bill, were absent.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from franklinwh_bridge.gateway.demand import DemandTracker

AGL_SEASONS = [{
    "id": "peak", "name": "Peak season", "months": [],
    "waves": {
        "super_off_peak": {"buy": 0.21626, "sell": 0.03},
        "off_peak": {"buy": 0.21626, "sell": 0.03},
        "mid_peak": {"buy": 0.54175, "sell": 0.28},
        "on_peak": {"buy": 0.54175, "sell": 0.03},
    },
    "blocks": [
        {"start": "00:00", "end": "15:00", "wave": "off_peak"},
        {"start": "15:00", "end": "17:00", "wave": "on_peak"},
        {"start": "17:00", "end": "21:00", "wave": "mid_peak"},
        {"start": "21:00", "end": "24:00", "wave": "off_peak"},
    ],
}]


class FakeBilling:
    """Only what the tracker asks of it."""

    def __init__(self, seasons=AGL_SEASONS):
        self._seasons = seasons

    def as_points(self):
        return {"tariff_seasons": self._seasons}

    def demand_config(self):
        return None

    def bonus_config(self):
        return None

    def charge_config(self):
        return None

    def plan(self):
        return {}


def sample(ts: dt.datetime, import_wh: float, export_wh: float = 0.0):
    return SimpleNamespace(
        gateway_id="default", ts=ts.timestamp(),
        points={"grid_import_wh": import_wh, "grid_export_wh": export_wh},
    )


@pytest.fixture
async def tracker(tmp_path):
    from franklinwh_bridge.store.db import init_db

    db = await init_db(tmp_path / "cost.db")
    t = DemandTracker(db, FakeBilling(), gateway_id="default")
    yield t
    await db.close()


@pytest.mark.asyncio
async def test_import_is_priced_at_the_live_rate(tracker):
    day = dt.datetime(2026, 1, 14)
    await tracker.on_sample(sample(day.replace(hour=10), 1000.0))
    # 10 kWh imported during off-peak (0.21626)
    await tracker.on_sample(sample(day.replace(hour=11), 11000.0))

    pts = tracker.as_points(now=day.replace(hour=11))

    assert pts["energy_import_cost"] == pytest.approx(10 * 0.21626, rel=1e-6)


@pytest.mark.asyncio
async def test_cost_integrates_across_a_wave_boundary(tracker):
    """The reason cost is integrated rather than multiplied at period end: a
    period spans waves, and only the running total prices each kWh correctly."""
    day = dt.datetime(2026, 1, 14)
    await tracker.on_sample(sample(day.replace(hour=14), 0.0))
    await tracker.on_sample(sample(day.replace(hour=14, minute=59), 1000.0))   # 1 kWh off-peak
    await tracker.on_sample(sample(day.replace(hour=16), 2000.0))              # 1 kWh on-peak
    await tracker.on_sample(sample(day.replace(hour=18), 3000.0))              # 1 kWh mid-peak

    cost = tracker.as_points(now=day.replace(hour=18))["energy_import_cost"]

    # as_points rounds to 4dp for display; the accumulator itself keeps full
    # precision, so compare at the reported resolution.
    assert cost == pytest.approx(0.21626 + 0.54175 + 0.54175, abs=5e-5)


@pytest.mark.asyncio
async def test_export_earns_the_live_sell_rate(tracker):
    """AGL's 28c evening feed-in vs 3c the rest of the day."""
    day = dt.datetime(2026, 1, 14)
    await tracker.on_sample(sample(day.replace(hour=12), 0.0, 0.0))
    await tracker.on_sample(sample(day.replace(hour=13), 0.0, 2000.0))   # 2 kWh @ 0.03
    await tracker.on_sample(sample(day.replace(hour=18), 0.0, 3000.0))   # 1 kWh @ 0.28

    credit = tracker.as_points(now=day.replace(hour=18))["energy_export_credit"]

    assert credit == pytest.approx(2 * 0.03 + 1 * 0.28, rel=1e-6)


@pytest.mark.asyncio
async def test_an_unpriced_hour_is_counted_not_discounted(tmp_path):
    """A plan that prices no rate for an hour must not make that energy free —
    the kWh is recorded separately so the gap is visible."""
    from franklinwh_bridge.store.db import init_db

    db = await init_db(tmp_path / "gap.db")
    partial = [{"id": "s", "months": [], "waves": {"off_peak": {"buy": 0.2, "sell": 0.03}},
                "blocks": [{"start": "00:00", "end": "12:00", "wave": "off_peak"}]}]
    t = DemandTracker(db, FakeBilling(partial), gateway_id="default")

    day = dt.datetime(2026, 1, 14, 18)
    await t.on_sample(sample(day, 0.0))
    await t.on_sample(sample(day.replace(minute=30), 5000.0))   # 5 kWh in an unpriced hour

    pts = t.as_points(now=day.replace(minute=30))
    assert pts["energy_import_cost"] == 0.0
    assert pts["energy_unpriced_import_kwh"] == pytest.approx(5.0)
    await db.close()


@pytest.mark.asyncio
async def test_no_plan_configured_prices_nothing_and_says_so(tmp_path):
    from franklinwh_bridge.store.db import init_db

    db = await init_db(tmp_path / "none.db")
    t = DemandTracker(db, FakeBilling([]), gateway_id="default")

    day = dt.datetime(2026, 1, 14, 10)
    await t.on_sample(sample(day, 0.0))
    await t.on_sample(sample(day.replace(hour=11), 4000.0))

    pts = t.as_points(now=day.replace(hour=11))
    assert pts["energy_import_cost"] == 0.0
    assert pts["energy_unpriced_import_kwh"] == pytest.approx(4.0)
    await db.close()


@pytest.mark.asyncio
async def test_a_meter_reset_does_not_invent_cost(tracker):
    """A counter going backwards is a reset, not a huge negative purchase."""
    day = dt.datetime(2026, 1, 14)
    await tracker.on_sample(sample(day.replace(hour=10), 9000.0))
    await tracker.on_sample(sample(day.replace(hour=11), 100.0))   # counter reset

    assert tracker.as_points(now=day.replace(hour=11))["energy_import_cost"] == 0.0


# ── Tiered pricing over a period ──────────────────────────────


TIERED_SEASONS = [{
    "id": "t", "name": "Tiered", "months": [],
    "waves": {"off_peak": {
        "buy": [{"up_to_kwh": 10, "rate": 0.20}, {"rate": 0.40}],
        "sell": 0.03,
    }},
    "blocks": [{"start": "00:00", "end": "24:00", "wave": "off_peak"}],
}]


@pytest.mark.asyncio
async def test_cost_switches_tier_as_the_period_accumulates(tmp_path):
    """15 kWh over a 10 kWh threshold: the first 10 at 0.20, the rest at 0.40.

    Sampled in 1 kWh steps so each delta is priced at the tier in force when it
    was consumed — which is the point of integrating rather than multiplying at
    the end.
    """
    from franklinwh_bridge.store.db import init_db

    db = await init_db(tmp_path / "tier.db")
    t = DemandTracker(db, FakeBilling(TIERED_SEASONS), gateway_id="default")

    day = dt.datetime(2026, 1, 14, 0, 0)
    await t.on_sample(sample(day, 0.0))
    for kwh in range(1, 16):
        await t.on_sample(sample(day + dt.timedelta(minutes=kwh), kwh * 1000.0))

    pts = t.as_points(now=day + dt.timedelta(minutes=15))
    assert pts["energy_period_import_kwh"] == pytest.approx(15.0)
    assert pts["energy_import_cost"] == pytest.approx(10 * 0.20 + 5 * 0.40, abs=5e-4)
    await db.close()


@pytest.mark.asyncio
async def test_cumulative_import_is_tracked_for_tiering(tmp_path):
    from franklinwh_bridge.store.db import init_db

    db = await init_db(tmp_path / "cum.db")
    t = DemandTracker(db, FakeBilling(), gateway_id="default")

    day = dt.datetime(2026, 1, 14, 10)
    await t.on_sample(sample(day, 1000.0))
    await t.on_sample(sample(day.replace(hour=11), 4000.0))

    assert t.as_points(now=day.replace(hour=11))["energy_period_import_kwh"] == pytest.approx(3.0)
    await db.close()
