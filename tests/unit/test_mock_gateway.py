"""Mock gateway: synthetic data, no Modbus connection, no contention."""

import asyncio
import time

import pytest

from franklinwh_bridge.gateway.instance import GatewayConfig, GatewayInstance
from franklinwh_bridge.gateway.mock_gateway import (
    MockController,
    mock_serial,
    synthetic_points,
)
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.store.db import (
    create_gateway,
    delete_gateway,
    get_gateway,
    init_db,
)


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


def test_synthetic_points_valid_late_at_night():
    """Regression: soc_curve's sin(...) ** 0.7 raised on a complex number for
    any hour past ~20:00 UTC, since sin() goes negative once its (unclamped)
    output -- not just its input -- needs clamping before a fractional power.
    Pin an affected hour (22:26 UTC) so this can't silently regress with the
    time of day again."""
    ts = 22 * 3600 + 26 * 60  # 22:26 UTC, day-of-epoch irrelevant (mod 86400)
    pts = synthetic_points("gw_night", 7, ts=ts)
    assert isinstance(pts["soc"], float)
    assert 0 <= pts["soc"] <= 100


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


async def test_delete_gateway_purges_its_metrics(db):
    await create_gateway(db, gateway_id="m2", name="Mock 2", host="", mock=True)
    for i in range(3):
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, gateway_id) VALUES (?, ?, ?)",
            (time.time() + i, 100, "m2"),
        )
    await db.commit()

    async with db.execute(
        "SELECT COUNT(*) FROM metrics WHERE gateway_id = 'm2'"
    ) as cur:
        assert (await cur.fetchone())[0] == 3

    assert await delete_gateway(db, "m2") is True

    async with db.execute(
        "SELECT COUNT(*) FROM metrics WHERE gateway_id = 'm2'"
    ) as cur:
        assert (await cur.fetchone())[0] == 0


async def test_delete_gateway_with_catalog(db):
    """A gateway that captured a SunSpec catalog (device_models/device_points,
    FK to gateways with no cascade) must still delete without a FOREIGN KEY
    constraint error."""
    await create_gateway(db, gateway_id="m3", name="Mock 3", host="1.2.3.4")
    cur = await db.execute(
        "INSERT INTO device_models (gateway_id, model_id, label, captured_at, hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m3", 1, "Common", 0.0, "h"),
    )
    model_db_id = cur.lastrowid
    await db.execute(
        "INSERT INTO device_points (model_db_id, point_name, addr) VALUES (?, ?, ?)",
        (model_db_id, "ID", 40003),
    )
    await db.commit()

    assert await delete_gateway(db, "m3") is True
    assert await get_gateway(db, "m3") is None
    async with db.execute(
        "SELECT COUNT(*) FROM device_models WHERE gateway_id = 'm3'"
    ) as cur:
        assert (await cur.fetchone())[0] == 0


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


async def test_health_checker_skips_tcp_probe_for_mock(monkeypatch):
    """A mock gateway must NOT be TCP-probed — that would time out against its
    synthetic host and mislabel it 'unreachable' though the mock polls fine."""
    from types import SimpleNamespace

    from franklinwh_bridge.gateway.health import HealthChecker

    def _inst(mock, host):
        return SimpleNamespace(
            config=SimpleNamespace(enabled=True, mock=mock, host=host, port=502),
            status=SimpleNamespace(health="unknown", connected=True, polling=True),
        )

    mock_inst = _inst(True, "192.168.0.250")
    real_inst = _inst(False, "10.255.255.1")
    reg = SimpleNamespace(
        instances={"m": mock_inst, "r": real_inst},
        get=lambda gid: {"m": mock_inst, "r": real_inst}.get(gid),
    )
    hc = HealthChecker(reg)
    probed = []

    async def fake_probe(host, port):
        probed.append(host)
        return False  # every real probe fails → unreachable

    monkeypatch.setattr(hc, "_tcp_probe", fake_probe)
    await hc._check_all()

    assert mock_inst.status.health == "connected"  # reflects polling, never probed
    assert real_inst.status.health == "unreachable"  # probed + failed
    assert "192.168.0.250" not in probed  # mock host never TCP-probed
    assert "10.255.255.1" in probed
    # check_one honours mock too
    assert await hc.check_one("m") == "connected"
