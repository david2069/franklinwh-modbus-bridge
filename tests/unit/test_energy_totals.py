"""Unit tests for period energy totals (today/week/month/YTD counter deltas)."""

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.energy_totals import EnergyTotals, period_start
from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "e.db")
    yield conn
    await conn.close()


def sample(gw, ts, points):
    return Sample(gateway_id=gw, ts=ts, points=points, quality="ok")


# ── period_start boundaries ───────────────────────────────────


def test_period_start_boundaries():
    # 2026-06-17 is a Wednesday 14:30
    now = datetime(2026, 6, 17, 14, 30, 45)
    assert period_start("today", now) == datetime(2026, 6, 17, 0, 0)
    assert period_start("this_week", now) == datetime(2026, 6, 15, 0, 0)  # Monday
    assert period_start("this_month", now) == datetime(2026, 6, 1, 0, 0)
    assert period_start("ytd", now) == datetime(2026, 1, 1, 0, 0)


# ── delta computation ─────────────────────────────────────────


async def test_totals_start_at_zero_then_accrue(db):
    now = datetime(2026, 6, 17, 14, 30)
    et = EnergyTotals(db, now_fn=lambda: now)
    # first sample sets the baseline → all periods read 0
    await et.on_sample(sample("default", 1000.0, {"grid_export_wh": 4_000_000}))
    assert et.current_totals("default")["energy_grid_export_today_kwh"] == 0.0
    # counter advances 12.5 kWh → every period delta = 12.5
    await et.on_sample(sample("default", 1030.0, {"grid_export_wh": 4_012_500}))
    t1 = et.current_totals("default")
    assert t1["energy_grid_export_today_kwh"] == 12.5
    assert t1["energy_grid_export_this_week_kwh"] == 12.5
    assert t1["energy_grid_export_ytd_kwh"] == 12.5


async def test_all_sources_present(db):
    now = datetime(2026, 6, 17, 12, 0)
    et = EnergyTotals(db, now_fn=lambda: now)
    pts = {
        "grid_import_wh": 1_000_000,
        "grid_export_wh": 2_000_000,
        "pv_energy_total_wh": 3_000_000,
        "dc_energy_charged_wh": 4_000_000,
        "dc_energy_discharged_wh": 5_000_000,
    }
    await et.on_sample(sample("default", 1000.0, pts))
    t = et.current_totals("default")
    for src in ("grid_import", "grid_export", "solar", "battery_charge", "battery_discharge"):
        for per in ("today", "this_week", "this_month", "ytd"):
            assert f"energy_{src}_{per}_kwh" in t


# ── roll-over at a period boundary ────────────────────────────


async def test_today_rolls_over_at_midnight(db):
    clock = {"now": datetime(2026, 6, 17, 23, 0)}  # late on the 17th
    et = EnergyTotals(db, now_fn=lambda: clock["now"])
    await et.on_sample(sample("default", 1000.0, {"pv_energy_total_wh": 10_000_000}))
    await et.on_sample(sample("default", 1030.0, {"pv_energy_total_wh": 10_005_000}))  # +5 kWh
    assert et.current_totals("default")["energy_solar_today_kwh"] == 5.0

    # cross into the next day → today re-bases; ytd keeps accruing
    clock["now"] = datetime(2026, 6, 18, 1, 0)
    await et.on_sample(
        sample("default", 1100.0, {"pv_energy_total_wh": 10_006_000})
    )  # +1 kWh past midnight
    t = et.current_totals("default")
    assert t["energy_solar_today_kwh"] == 1.0  # only the new day
    assert t["energy_solar_ytd_kwh"] == 6.0  # whole run since first sample


# ── counter reset re-bases (never negative) ───────────────────


async def test_counter_reset_rebases(db):
    now = datetime(2026, 6, 17, 12, 0)
    et = EnergyTotals(db, now_fn=lambda: now)
    await et.on_sample(sample("default", 1000.0, {"grid_import_wh": 9_000_000}))
    await et.on_sample(sample("default", 1030.0, {"grid_import_wh": 9_010_000}))  # +10 kWh
    assert et.current_totals("default")["energy_grid_import_today_kwh"] == 10.0
    # firmware reset → counter drops; must re-base, not go negative
    await et.on_sample(sample("default", 1060.0, {"grid_import_wh": 500}))
    t = et.current_totals("default")
    assert t["energy_grid_import_today_kwh"] == 0.0
    await et.on_sample(sample("default", 1090.0, {"grid_import_wh": 2500}))  # +2 kWh
    assert et.current_totals("default")["energy_grid_import_today_kwh"] == 2.0


# ── persistence across restart ────────────────────────────────


async def test_baselines_persist_across_restart(db):
    now = datetime(2026, 6, 17, 12, 0)
    et = EnergyTotals(db, now_fn=lambda: now)
    await et.on_sample(sample("default", 1000.0, {"grid_export_wh": 4_000_000}))  # baseline
    # new instance (simulated restart) loads persisted baselines
    et2 = EnergyTotals(db, now_fn=lambda: now)
    await et2.on_sample(sample("default", 2000.0, {"grid_export_wh": 4_020_000}))  # +20 kWh
    assert et2.current_totals("default")["energy_grid_export_today_kwh"] == 20.0


# ── sensor registry integration ───────────────────────────────


def test_period_sensors_registered():
    from franklinwh_bridge.gateway.scheduler_sensors import snapshot

    pts = {"energy_grid_export_this_month_kwh": 123.4, "energy_solar_ytd_kwh": 999.9}
    snap = snapshot(pts, datetime(2026, 6, 17, 12, 0))
    assert snap["energy.grid_export.this_month_kwh"] == 123.4
    assert snap["energy.solar.ytd_kwh"] == 999.9
    assert snap["energy.battery_charge.today_kwh"] is None  # absent → None
