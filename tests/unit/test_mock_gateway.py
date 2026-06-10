"""Mock gateway: synthetic data, no Modbus connection, no contention."""

import asyncio

import pytest

from franklinwh_bridge.gateway.instance import GatewayConfig, GatewayInstance
from franklinwh_bridge.gateway.mock_gateway import (
    MockController,
    mock_serial,
    synthetic_points,
)
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.store.db import create_gateway, get_gateway, init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "mock.db")
    yield conn
    await conn.close()


def test_synthetic_points_has_canonical_keys():
    pts = synthetic_points("gw_demo", 3)
    for k in (
        "soc", "battery_power_w", "battery_dc_power_w", "grid_power_w",
        "total_solar", "home_load_ext", "battery_state", "connection_state",
    ):
        assert k in pts
    assert 0 <= pts["soc"] <= 100
    assert pts["battery_state"] in ("Charging", "Discharging", "Standby")


def test_synthetic_points_differ_per_gateway():
    # Distinct phase offset → distinct curves, so mocks don't look identical.
    assert synthetic_points("alpha", 5) != synthetic_points("bravo", 5)


def test_mock_controller_nameplate():
    c = MockController("gw2")
    assert c.connect() is True
    np = c.read_nameplate()
    assert np["serial"] == mock_serial("gw2") == "MOCK-GW2"
    assert "mock" in np["model"].lower()


async def test_migration_persists_mock_flag(db):
    gw = await create_gateway(db, gateway_id="m1", name="Mock 1", host="", mock=True)
    assert gw["mock"] == 1
    assert (await get_gateway(db, "m1"))["mock"] == 1


async def test_mock_instance_emits_without_hardware(db):
    await create_gateway(db, gateway_id="mock1", name="Mock 1", host="", mock=True)
    bus = SampleBus()
    cfg = GatewayConfig(
        gateway_id="mock1", name="Mock 1", host="", mock=True, poll_interval=1
    )
    inst = GatewayInstance(cfg, db, global_bus=bus)
    try:
        await inst.start()
        await asyncio.sleep(0.1)  # let the first synthetic sample publish

        # Mock controller, no real connection, synthetic serial.
        assert isinstance(inst.controller, MockController)
        assert inst.status.connected is True
        assert inst.status.polling is True
        assert inst.status.serial == "MOCK-MOCK1"

        # Per-gateway bus got a sample with canonical keys...
        s = inst.sample_bus.last_sample
        assert s is not None and "soc" in s.points
        # ...and it fanned out to the global bus (for Site aggregation).
        assert bus.last_sample is not None
        assert bus.last_sample.gateway_id == "mock1"
    finally:
        await inst.stop()
