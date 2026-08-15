"""Demand-charge + battery-bonus calculator (DemandTracker)."""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.demand import DemandTracker, _period_start
from franklinwh_bridge.modbus.sample import Sample

ALLDAY = {"months": [], "days": [], "start": "00:00", "end": "23:59"}


class FakeBilling:
    def __init__(self, demand=None, bonus=None):
        self._d, self._b = demand, bonus

    def demand_config(self):
        return self._d

    def bonus_config(self):
        return self._b


def _s(dt: datetime, **points) -> Sample:
    return Sample(gateway_id="default", ts=dt.timestamp(), points=points, quality="ok")


async def _feed(tracker, samples):
    for s in samples:
        await tracker.on_sample(s)


def _demand_cfg(**over):
    base = {"window": ALLDAY, "rate": 0.5, "cycle_day": 1, "interval_min": 30,
            "charge_basis": "per_kw_day"}
    base.update(over)
    return base


async def test_peak_demand_30min_x2():
    t = DemandTracker(db=None, billing=FakeBilling(demand=_demand_cfg()))
    d = datetime(2026, 3, 10)  # well into a period (cycle day 1)
    await _feed(t, [
        _s(d.replace(hour=10, minute=0), grid_import_wh=0),
        _s(d.replace(hour=10, minute=29), grid_import_wh=1000),   # 1 kWh in [10:00]
        _s(d.replace(hour=10, minute=30), grid_import_wh=1000),   # close [10:00]=1.0
        _s(d.replace(hour=11, minute=0), grid_import_wh=4000),    # close [10:30]=3.0
    ])
    pts = t.as_points(now=d.replace(hour=11, minute=0))
    assert pts["demand_peak_kw"] == 6.0   # max interval 3 kWh × 2
    assert pts["demand_days_in_period"] == pytest.approx(9 + 11 / 24, abs=0.01)


async def test_configurable_interval_15min_x4():
    t = DemandTracker(db=None, billing=FakeBilling(demand=_demand_cfg(interval_min=15)))
    d = datetime(2026, 3, 10)
    await _feed(t, [
        _s(d.replace(hour=10, minute=0), grid_import_wh=0),
        _s(d.replace(hour=10, minute=15), grid_import_wh=1000),   # close [10:00]=1 kWh
        _s(d.replace(hour=10, minute=30), grid_import_wh=1000),
    ])
    assert t.as_points(now=d.replace(hour=10, minute=30))["demand_peak_kw"] == 4.0  # 1×4


async def test_charge_basis_and_bonus():
    from franklinwh_bridge.gateway.scheduler_sensors import snapshot
    t = DemandTracker(db=None, billing=FakeBilling(
        demand=_demand_cfg(rate=0.4), bonus={"window": ALLDAY, "rate": 0.28}))
    d = datetime(2026, 3, 10)
    await _feed(t, [
        _s(d.replace(hour=10, minute=0), grid_import_wh=0, grid_export_wh=0),
        _s(d.replace(hour=10, minute=30), grid_import_wh=2000, grid_export_wh=8000),  # 2kWh/8kWh
        _s(d.replace(hour=11, minute=0), grid_import_wh=2000, grid_export_wh=8000),
    ])
    now = d.replace(hour=11, minute=0)
    pts = {**t.as_points(now=now), "tariff_demand_rate": 0.4,
           "tariff_export_bonus_rate": 0.28, "tariff_demand_charge_basis": "per_kw_day"}
    snap = snapshot(pts, now)
    assert snap["demand.peak_kw"] == 4.0            # 2 kWh × 2
    assert snap["bonus.export_kwh"] == 8.0
    assert snap["bonus.period_credit"] == round(8.0 * 0.28, 2)   # $2.24
    # per_kw_day = 4 kW × 0.4 × days
    days = pts["demand_days_in_period"]
    assert snap["demand.period_charge"] == round(4.0 * 0.4 * days, 2)
    # flat basis ignores days
    snap2 = snapshot({**pts, "tariff_demand_charge_basis": "flat_per_kw"}, now)
    assert snap2["demand.period_charge"] == round(4.0 * 0.4, 2)


async def test_bonus_only_in_window():
    win = {"months": [], "days": [], "start": "17:00", "end": "21:00"}
    t = DemandTracker(db=None, billing=FakeBilling(bonus={"window": win, "rate": 0.28}))
    d = datetime(2026, 3, 10)
    await _feed(t, [
        _s(d.replace(hour=12, minute=0), grid_export_wh=0),       # outside window
        _s(d.replace(hour=12, minute=30), grid_export_wh=5000),   # +5 outside → ignored
        _s(d.replace(hour=18, minute=0), grid_export_wh=5000),    # enter window
        _s(d.replace(hour=19, minute=0), grid_export_wh=9000),    # +4 inside → counts
    ])
    assert t.as_points()["bonus_export_kwh"] == 4.0


async def test_period_rollover_resets():
    t = DemandTracker(db=None, billing=FakeBilling(demand=_demand_cfg(cycle_day=1)))
    m1 = datetime(2026, 3, 20)
    await _feed(t, [
        _s(m1.replace(hour=10, minute=0), grid_import_wh=0),
        _s(m1.replace(hour=10, minute=30), grid_import_wh=3000),
        _s(m1.replace(hour=11, minute=0), grid_import_wh=3000),   # peak 3 kWh
    ])
    assert t.as_points()["demand_peak_kw"] == 6.0
    # cross into April (cycle day 1) → reset
    m2 = datetime(2026, 4, 2, 10, 0)
    await t.on_sample(_s(m2, grid_import_wh=3000))
    assert t.as_points()["demand_peak_kw"] == 0.0


def test_period_start():
    # cycle day 12; now = Mar 5 → period started Feb 12
    assert _period_start(datetime(2026, 3, 5), 12) == datetime(2026, 2, 12)
    # now = Mar 20 → period started Mar 12
    assert _period_start(datetime(2026, 3, 20), 12) == datetime(2026, 3, 12)
