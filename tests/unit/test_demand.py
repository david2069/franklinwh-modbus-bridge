"""Demand-charge + battery-bonus calculator (DemandTracker)."""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.demand import DemandTracker, _period_start
from franklinwh_bridge.modbus.sample import Sample

ALLDAY = {"months": [], "days": [], "start": "00:00", "end": "23:59"}


class FakeBilling:
    def __init__(self, demand=None, bonus=None, charge=None):
        self._d, self._b, self._c = demand, bonus, charge

    def demand_config(self):
        return self._d

    def bonus_config(self):
        return self._b

    def charge_config(self):
        return self._c


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


async def test_export_charge_window_and_free_allowance():
    """Charge-window export accumulates; free allowance = free_kwh_per_day ×
    full-period days; net + cost only above the free threshold."""
    from franklinwh_bridge.gateway.scheduler_sensors import snapshot
    charge = {"window": ALLDAY, "rate": 0.0123, "free_kwh_per_day": 6.84, "cycle_day": 1}

    class B(FakeBilling):
        def charge_config(self):
            return charge

    t = DemandTracker(db=None, billing=B())
    d = datetime(2026, 3, 10)  # March = 31-day period (cycle day 1) → free ≈ 6.84×31
    await _feed(t, [
        _s(d.replace(hour=11, minute=0), grid_export_wh=0),
        _s(d.replace(hour=12, minute=0), grid_export_wh=300000),   # +300 kWh in-window
    ])
    now = d.replace(hour=12, minute=0)
    pts = {**t.as_points(now=now), "tariff_export_charge_rate": 0.0123}
    snap = snapshot(pts, now)
    free = round(6.84 * 31, 3)                       # 212.04 kWh free this period
    assert snap["tariff.export_charge_kwh"] == 300.0
    assert snap["tariff.export_charge_free_remaining"] == 0.0     # exceeded free
    assert snap["tariff.export_charge_net_kwh"] == round(300.0 - free, 3)
    assert snap["tariff.export_charge_cost"] == round((300.0 - free) * 0.0123, 2)


async def test_export_charge_under_free_costs_nothing():
    from franklinwh_bridge.gateway.scheduler_sensors import snapshot
    charge = {"window": ALLDAY, "rate": 0.0123, "free_kwh_per_day": 6.84, "cycle_day": 1}

    class B(FakeBilling):
        def charge_config(self):
            return charge

    t = DemandTracker(db=None, billing=B())
    d = datetime(2026, 3, 10)
    await _feed(t, [
        _s(d.replace(hour=11, minute=0), grid_export_wh=0),
        _s(d.replace(hour=12, minute=0), grid_export_wh=50000),   # 50 kWh < free
    ])
    now = d.replace(hour=12, minute=0)
    snap = snapshot({**t.as_points(now=now), "tariff_export_charge_rate": 0.0123}, now)
    assert snap["tariff.export_charge_net_kwh"] == 0.0
    assert snap["tariff.export_charge_cost"] == 0.0
    assert snap["tariff.export_charge_free_remaining"] > 100  # plenty left
