"""User-defined automation constants: store clamping/persistence + derived sensors."""

from __future__ import annotations

import pytest

from franklinwh_bridge.gateway.constants import ConstantsStore
from franklinwh_bridge.gateway.scheduler_sensors import snapshot
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "c.db")
    yield conn
    await conn.close()


async def test_defaults_and_persist(db):
    store = ConstantsStore(db)
    await store.load()
    assert store.values() == {
        "min_discharge_soc": 20.0, "max_charge_soc": 100.0, "demand_charge_min_soc": 30.0,
    }
    # update + clamp (150 → 100, -5 → 0), unknown key ignored
    await store.update({"max_charge_soc": 150, "min_discharge_soc": -5, "bogus": 1})
    assert store.values()["max_charge_soc"] == 100.0
    assert store.values()["min_discharge_soc"] == 0.0

    # persists across a reload
    store2 = ConstantsStore(db)
    await store2.load()
    assert store2.values()["max_charge_soc"] == 100.0
    assert store2.values()["min_discharge_soc"] == 0.0

    # as_points keys are const_*
    assert store2.as_points()["const_max_charge_soc"] == 100.0


def test_derived_sensors_math():
    pts = {
        "wh_rating": 13600, "soc": 50.0, "battery_power_w": -2000,
        "max_charge_rate_w": 5000, "max_discharge_rate_w": 5000,
        "const_min_discharge_soc": 20, "const_max_charge_soc": 100,
    }
    s = snapshot(pts)
    assert s["battery.capacity_kwh"] == 13.6
    assert s["battery.stored_kwh"] == 6.8
    assert s["battery.remaining_kwh"] == 6.8
    assert s["inverter.power_rating_w"] == 5000.0
    assert s["inverter.utilised_w"] == 2000.0
    assert s["inverter.unutilised_w"] == 3000.0
    assert s["battery.time_to_charge_min"] == 81.6      # (13.6-6.8)/5*60
    assert s["battery.time_to_discharge_min"] == 49.0   # (6.8-2.72)/5*60


def test_derived_fail_closed_on_missing_inputs():
    s = snapshot({})  # no points
    for k in ("battery.capacity_kwh", "battery.remaining_kwh", "inverter.power_rating_w",
              "battery.time_to_charge_min"):
        assert s[k] is None
