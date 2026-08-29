"""Fixed / standing charges accrual (FixedChargesStore)."""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.fixed_charges import FixedChargesStore, _daily_equiv


def _charge(**over):
    base = {"type": "supply", "description": "", "levied_by": "utility",
            "frequency": "daily", "rate": 1.0, "tax_rate": 0.0}
    base.update(over)
    return base


def test_daily_equiv_period_aligned():
    # Over a 30-day billing period, period-based cadences prorate to $1/day.
    assert _daily_equiv("daily", 1.85, 30) == 1.85
    assert _daily_equiv("weekly", 7.0, 30) == 1.0
    assert _daily_equiv("monthly", 30.0, 30) == pytest.approx(1.0)
    assert _daily_equiv("quarterly", 90.0, 30) == pytest.approx(1.0)
    assert _daily_equiv("annual", 360.0, 30) == pytest.approx(1.0)


async def test_accrual_midperiod():
    s = FixedChargesStore(db=None)
    s._cycle_day = 1
    s._charges = [
        _charge(frequency="daily", rate=1.85),          # Ausgrid supply+metering-ish
        _charge(frequency="monthly", rate=22.42, levied_by="retailer"),  # Amber membership
    ]
    now = datetime(2026, 3, 10)  # March = 31-day period, 9 days elapsed
    p = s.as_points(now=now)
    daily = 1.85 + 22.42 / 31
    assert p["fixed_daily_charge"] == pytest.approx(daily, abs=1e-4)
    assert p["fixed_period_total"] == pytest.approx(1.85 * 31 + 22.42, abs=0.02)
    assert p["fixed_accrued_period"] == pytest.approx(daily * 9, abs=0.02)
    assert p["fixed_days_remaining"] == pytest.approx(22.0, abs=1e-3)
    assert p["fixed_remaining"] == pytest.approx(
        p["fixed_period_total"] - p["fixed_accrued_period"], abs=0.02
    )


async def test_tax_inclusive():
    s = FixedChargesStore(db=None)
    s._cycle_day = 1
    s._charges = [_charge(frequency="daily", rate=1.0, tax_rate=0.10)]
    p = s.as_points(now=datetime(2026, 3, 10))
    assert p["fixed_daily_charge"] == pytest.approx(1.10)


async def test_load_from_service_pricing(tmp_path):
    from franklinwh_bridge.store.db import create_service, init_db, update_service

    db = await init_db(tmp_path / "f.db")
    try:
        svc = await create_service(db, "Svc")
        await update_service(
            db, svc["id"],
            pricing={
                "billing_cycle_day": 1,
                "fixed_charges": [
                    {"type": "supply", "description": "Network daily",
                     "levied_by": "utility", "frequency": "daily",
                     "rate": 1.85, "tax_rate": 0.0},
                ],
            },
        )
        store = FixedChargesStore(db)
        await store.load()
        assert len(store.charges()) == 1
        assert store.charges()[0]["rate"] == 1.85
        assert store.charges()[0]["levied_by"] == "utility"
        p = store.as_points(now=datetime(2026, 3, 10))
        assert p["fixed_daily_charge"] == pytest.approx(1.85)
    finally:
        await db.close()
