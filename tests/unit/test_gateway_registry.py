"""Tests for GatewayRegistry and GatewayInstance lifecycle."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from franklinwh_bridge.gateway.instance import GatewayConfig, GatewayInstance
from franklinwh_bridge.gateway.registry import GatewayRegistry
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.store.db import create_gateway, init_db


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await init_db(db_path)
    # Create default gateway (normally done by main.py lifespan)
    await create_gateway(
        conn, gateway_id="default", name="Default Gateway",
        host="192.168.1.100", port=502, unit_id=1,
    )
    yield conn
    await conn.close()


@pytest.fixture
def global_bus():
    return SampleBus()


@pytest.fixture
def mock_controller_class():
    """Patch FranklinWHController so GatewayInstance doesn't need hardware."""
    ctrl = MagicMock()
    ctrl.ip_address = "192.168.1.100"
    ctrl.port = 502
    ctrl.unit_id = 1
    ctrl.connect.return_value = True
    ctrl.disconnect.return_value = None
    ctrl.read_nameplate.return_value = {
        "serial": "TEST00000001",
        "manufacturer": "FranklinWH",
        "model": "aGate X",
        "version": "1.0.0",
    }
    ctrl.read_battery_status.return_value = {"soc": 75, "power": -500}
    ctrl.read_grid_status.return_value = {"grid_power": 200}
    ctrl.read_solar_status.return_value = {"pv_power": 3000}
    ctrl.read_control_status.return_value = {
        "wset_enabled": 0, "wset_pct": 0, "loc_rem_ctl_name": "Local",
    }
    ctrl.read_native_mode.return_value = {"native_mode": 2}
    ctrl.read_alarms.return_value = {}
    ctrl.get_model.return_value = None
    ctrl.reset_control_state.return_value = True
    ctrl.dev = MagicMock()
    return ctrl


# ── GatewayConfig ─────────────────────────────────────────────


def test_gateway_config_defaults():
    cfg = GatewayConfig(gateway_id="test", name="Test", host="192.168.1.1")
    assert cfg.port == 502
    assert cfg.unit_id == 1
    assert cfg.poll_interval == 10
    assert cfg.enabled is True


# ── GatewayInstance ───────────────────────────────────────────


async def test_instance_to_dict():
    cfg = GatewayConfig(
        gateway_id="gw1", name="Gateway 1", host="10.0.0.1",
        description="Phase 1",
    )
    inst = GatewayInstance(
        config=cfg, db=MagicMock(), global_bus=SampleBus(),
    )
    d = inst.to_dict()
    assert d["gateway_id"] == "gw1"
    assert d["name"] == "Gateway 1"
    assert d["host"] == "10.0.0.1"
    assert d["description"] == "Phase 1"
    assert d["connected"] is False
    assert d["polling"] is False


async def test_instance_start_creates_poller(db, global_bus, mock_controller_class):
    cfg = GatewayConfig(
        gateway_id="default", name="Default", host="192.168.1.100",
    )
    inst = GatewayInstance(config=cfg, db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
        return_value=mock_controller_class,
    ):
        await inst.start()
        # Wait for _init_and_poll to run
        await asyncio.sleep(0.5)

    assert inst.controller is not None
    assert inst.poller is not None
    assert inst.command_handler is not None
    assert inst.modbus_lock is not None

    await inst.stop()


async def test_instance_fan_in_to_global(db, global_bus, mock_controller_class):
    """Per-gateway samples should appear on the global bus."""
    cfg = GatewayConfig(
        gateway_id="default", name="Default", host="192.168.1.100",
    )
    inst = GatewayInstance(config=cfg, db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
        return_value=mock_controller_class,
    ):
        await inst.start()
        await asyncio.sleep(0.5)

    # Publish a sample to the per-gateway bus
    from franklinwh_bridge.modbus.sample import Sample

    sample = Sample.now("default", {"soc": 50})
    await inst.sample_bus.publish(sample)

    # Should appear on global bus
    assert global_bus.last_sample is not None
    assert global_bus.last_sample.points.get("soc") == 50

    await inst.stop()


# ── GatewayRegistry ──────────────────────────────────────────


async def test_registry_start_all(db, global_bus):
    """Registry should start the default gateway."""
    registry = GatewayRegistry(db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
    ) as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False  # won't fully init, but registers
        MockCtrl.return_value = ctrl
        await registry.start_all()

    assert "default" in registry.list_all()
    assert registry.get("default") is not None

    await registry.stop_all()
    assert len(registry.list_all()) == 0


async def test_registry_start_stop_single(db, global_bus):
    """Start and stop a single gateway."""
    registry = GatewayRegistry(db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
    ) as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False
        MockCtrl.return_value = ctrl
        inst = await registry.start_gateway("default")

    assert inst is not None
    assert "default" in registry.list_all()

    await registry.stop_gateway("default")
    assert "default" not in registry.list_all()


async def test_registry_add_second_gateway(db, global_bus):
    """Add and start a second gateway alongside the default."""
    # Create a second gateway in DB
    await create_gateway(
        db, gateway_id="gw2", name="Gateway 2",
        host="192.168.1.101", port=502,
    )

    registry = GatewayRegistry(db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
    ) as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False
        MockCtrl.return_value = ctrl
        await registry.start_all()

    assert len(registry.list_all()) == 2
    assert "default" in registry.list_all()
    assert "gw2" in registry.list_all()

    await registry.stop_all()


async def test_start_all_skips_user_stopped_gateway(db, global_bus):
    """A gateway with autostart=0 (user-stopped) must NOT come back on boot.

    Regression for the mock gateway that self-restarted after Stop because the
    DB row stayed enabled=1 and start_all() restarted every enabled row.
    """
    from franklinwh_bridge.store.db import update_gateway

    # A stopped mock: still enabled (configured), but autostart cleared.
    await create_gateway(db, gateway_id="mock1", name="Mock 1", host="", mock=True)
    await update_gateway(db, "mock1", autostart=0)

    registry = GatewayRegistry(db=db, global_bus=global_bus)
    with patch("franklinwh_modbus.FranklinWHController") as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False
        MockCtrl.return_value = ctrl
        await registry.start_all()

    # default starts; the user-stopped mock does not.
    assert "default" in registry.list_all()
    assert "mock1" not in registry.list_all()

    await registry.stop_all()


async def test_registry_status_all(db, global_bus):
    """status_all returns a dict per registered gateway."""
    registry = GatewayRegistry(db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
    ) as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False
        MockCtrl.return_value = ctrl
        await registry.start_all()

    statuses = registry.status_all()
    assert len(statuses) == 1
    assert statuses[0]["gateway_id"] == "default"
    assert "connected" in statuses[0]
    assert "polling" in statuses[0]

    await registry.stop_all()


async def test_registry_get_default(db, global_bus):
    registry = GatewayRegistry(db=db, global_bus=global_bus)

    with patch(
        "franklinwh_modbus.FranklinWHController",
    ) as MockCtrl:
        ctrl = MagicMock()
        ctrl.connect.return_value = False
        MockCtrl.return_value = ctrl
        await registry.start_all()

    assert registry.get_default() is not None
    assert registry.get_default().gateway_id == "default"
    assert registry.get("nonexistent") is None

    await registry.stop_all()
