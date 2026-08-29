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

    def as_points(self):
        d = self._d or {}
        c = self._c or {}
        return {
            "tariff_demand_rate": d.get("rate", 0.0),
            "tariff_export_bonus_rate": (self._b or {}).get("rate", 0.0),
            "tariff_export_charge_rate": c.get("rate", 0.0),
            "tariff_demand_charge_basis": d.get("charge_basis", "per_kw_day"),
        }


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


async def test_rollover_snapshots_period_history(tmp_path):
    """Crossing a billing cycle writes the closing period's final tariff totals
    to billing_periods, matching the live sensor formulas."""
    from franklinwh_bridge.store.db import get_billing_periods, init_db

    db = await init_db(tmp_path / "hist.db")
    try:
        billing = FakeBilling(
            demand=_demand_cfg(rate=0.15),
            bonus={"window": ALLDAY, "rate": 0.0385},
            charge={"window": ALLDAY, "rate": 0.0123, "free_kwh_per_day": 6.84, "cycle_day": 1},
        )
        t = DemandTracker(db=db, billing=billing)
        m = datetime(2026, 3, 10)  # March = 31-day period (cycle day 1)
        await _feed(t, [
            _s(m.replace(hour=10, minute=0), grid_import_wh=0, grid_export_wh=0),
            _s(m.replace(hour=10, minute=30), grid_import_wh=3000, grid_export_wh=300000),
            _s(m.replace(hour=11, minute=0), grid_import_wh=3000, grid_export_wh=300000),
        ])
        # cross into April → snapshots the March period
        apr = datetime(2026, 4, 2, 10, 0)
        await t.on_sample(_s(apr, grid_import_wh=3000, grid_export_wh=300000))

        rows = await get_billing_periods(db, "default")
        assert len(rows) == 1
        r = rows[0]
        assert r["demand_peak_kw"] == 6.0                       # 3 kWh × 2
        assert r["demand_charge"] == round(6.0 * 0.15 * 31, 2)  # peak × rate × full days
        assert r["reward_kwh"] == 300.0
        assert r["reward_credit"] == round(300.0 * 0.0385, 2)
        assert r["charge_kwh"] == 300.0
        free = 6.84 * 31                                        # 212.04 free
        assert r["charge_net_kwh"] == round(300.0 - free, 2)
        assert r["charge_cost"] == round((300.0 - free) * 0.0123, 2)
        assert r["net_total"] == round(
            r["demand_charge"] + r["charge_cost"] - r["reward_credit"], 2
        )

        # Idempotent: re-processing the same rollover doesn't duplicate the row.
        apr2 = datetime(2026, 4, 2, 10, 1)
        await t.on_sample(_s(apr2, grid_import_wh=3000, grid_export_wh=300000))
        assert len(await get_billing_periods(db, "default")) == 1
    finally:
        await db.close()


async def test_rollover_snapshot_includes_fixed_charges(tmp_path):
    """Standing charges for the full closing period land in the snapshot and are
    added to net_total (they're a cost, like demand/export charges)."""
    from franklinwh_bridge.gateway.fixed_charges import FixedChargesStore
    from franklinwh_bridge.store.db import get_billing_periods, init_db

    db = await init_db(tmp_path / "fixed_hist.db")
    try:
        fixed = FixedChargesStore(db=None)
        fixed._cycle_day = 1
        fixed._charges = [
            {"type": "supply", "description": "", "levied_by": "utility",
             "frequency": "daily", "rate": 1.85, "tax_rate": 0.0},
        ]
        billing = FakeBilling(demand=_demand_cfg(rate=0.15))
        t = DemandTracker(db=db, billing=billing, fixed_charges=fixed)
        m = datetime(2026, 3, 10)  # March = 31-day period (cycle day 1)
        await _feed(t, [
            _s(m.replace(hour=10, minute=0), grid_import_wh=0),
            _s(m.replace(hour=10, minute=30), grid_import_wh=3000),
            _s(m.replace(hour=11, minute=0), grid_import_wh=3000),
        ])
        await t.on_sample(_s(datetime(2026, 4, 2, 10, 0), grid_import_wh=3000))

        r = (await get_billing_periods(db, "default"))[0]
        assert r["fixed_charges"] == round(1.85 * 31, 2)     # full period, not elapsed
        assert r["net_total"] == round(r["demand_charge"] + r["fixed_charges"], 2)
    finally:
        await db.close()


async def test_fixed_charges_alone_still_snapshot(tmp_path):
    """Standing charges with no demand/bonus/export tariff are still a bill —
    the rollover records the period rather than skipping it."""
    from franklinwh_bridge.gateway.fixed_charges import FixedChargesStore
    from franklinwh_bridge.store.db import get_billing_periods, init_db

    db = await init_db(tmp_path / "fixed_only.db")
    try:
        fixed = FixedChargesStore(db=None)
        fixed._cycle_day = 1
        fixed._charges = [
            {"type": "membership", "description": "", "levied_by": "retailer",
             "frequency": "monthly", "rate": 22.42, "tax_rate": 0.0},
        ]
        t = DemandTracker(db=db, billing=FakeBilling(), fixed_charges=fixed)
        await t.on_sample(_s(datetime(2026, 3, 10), grid_import_wh=1000))
        await t.on_sample(_s(datetime(2026, 4, 2), grid_import_wh=1000))  # rollover

        rows = await get_billing_periods(db, "default")
        assert len(rows) == 1
        assert rows[0]["fixed_charges"] == pytest.approx(22.42, abs=0.02)
        assert rows[0]["net_total"] == rows[0]["fixed_charges"]
    finally:
        await db.close()


async def test_no_snapshot_without_tariff(tmp_path):
    """A rollover with no tariff configured records nothing."""
    from franklinwh_bridge.store.db import get_billing_periods, init_db

    db = await init_db(tmp_path / "empty.db")
    try:
        t = DemandTracker(db=db, billing=FakeBilling())  # no demand/bonus/charge
        await t.on_sample(_s(datetime(2026, 3, 10), grid_import_wh=1000))
        await t.on_sample(_s(datetime(2026, 4, 2), grid_import_wh=1000))  # rollover
        assert await get_billing_periods(db, "default") == []
    finally:
        await db.close()


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
