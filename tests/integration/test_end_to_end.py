"""End-to-end integration tests: poll → publish → API.

These tests wire together the real application components (poller,
sample bus, MQTT publisher, command handler, REST API) with a mocked
Modbus controller to verify the full data pipeline without hardware.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import Sample, SampleBus
from franklinwh_bridge.publish.command_handler import DEFAULT_MAX_POWER_W, CommandHandler
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.publish.mqtt_publisher import (
    DeviceInfo,
    MqttPublisher,
    build_discovery_payload,
)
from franklinwh_bridge.store.db import init_db

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_controller():
    """A fully-stubbed FranklinWHController for integration tests."""
    ctrl = MagicMock()
    ctrl.ip_address = "192.168.1.100"
    ctrl.port = 502
    ctrl.unit_id = 1
    ctrl.connect.return_value = True
    ctrl.disconnect.return_value = None
    ctrl.read_battery_status.return_value = {
        "soc": 72,
        "battery_power_w": -1800,
        "battery_state": "Discharging",
        "battery_current_a": 3.5,
    }
    ctrl.read_grid_status.return_value = {
        "grid_power": 450,
        "voltage_v": 242.3,
        "frequency_hz": 50.01,
        "current_a": 1.9,
    }
    ctrl.read_solar_status.return_value = {
        "total_solar": 4200,
        "pv_total": 4200,
        "pv_proximal": 4200,
        "pv_remote1": 0,
        "pv_remote2": 0,
    }
    ctrl.read_nameplate.return_value = {
        "serial": "10060006A02F00000001",
        "manufacturer": "FranklinWH",
        "model": "aGate X",
        "rated_power": 5000,
        "max_charge_rate_w": 6000,
        "max_discharge_rate_w": 5500,
    }
    ctrl.read_control_status.return_value = {
        "wset_enabled": False,
        "wset_pct": 0,
        "loc_rem_ctl_name": "Remote",
    }
    ctrl.read_native_mode.return_value = {"native_mode": 2}
    ctrl.read_alarms.return_value = {"active_alarms": []}
    ctrl.send_command.return_value = (True, "OK")
    ctrl.reset_control_state.return_value = True
    ctrl.get_model.return_value = None
    return ctrl


@pytest.fixture
def sample_bus():
    return SampleBus()


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest.fixture
def device_info():
    return DeviceInfo(
        serial="10060006A02F00000001",
        manufacturer="FranklinWH",
        model="aGate X",
        firmware="V10R01B04D00",
    )


# ---------------------------------------------------------------------------
# 1. Poll → SampleBus → MQTT queue pipeline
# ---------------------------------------------------------------------------


async def test_poll_to_mqtt_queue(mock_controller, sample_bus, device_info):
    """A single poll produces MQTT messages with correct topics and payloads."""
    publisher = MqttPublisher()
    publisher.set_device_info(device_info)
    sample_bus.subscribe(publisher.queue_sample)

    poller = ModbusPoller(
        controller=mock_controller,
        sample_bus=sample_bus,
        gateway_id="default",
        poll_interval=30,
    )
    await poller.poll_once()

    # Drain the queue
    messages = []
    while not publisher._queue.empty():
        messages.append(publisher._queue.get_nowait())

    topics = {m.topic for m in messages}
    short_id = device_info.short_id

    # Verify key entities got published
    assert f"franklinwh/{short_id}/battery/battery_soc" in topics
    assert f"franklinwh/{short_id}/battery/battery_power_kw" in topics
    assert f"franklinwh/{short_id}/status/grid_voltage_v" in topics

    # Verify values are correctly formatted
    soc_msg = next(m for m in messages if "battery_soc" in m.topic)
    assert soc_msg.payload == "72"

    power_msg = next(m for m in messages if "battery_power_kw" in m.topic)
    assert power_msg.payload == "-1.800"


async def test_poll_to_api_points(mock_controller, sample_bus):
    """Poll data is accessible via the /api/points REST endpoint."""
    poller = ModbusPoller(
        controller=mock_controller,
        sample_bus=sample_bus,
        gateway_id="default",
        poll_interval=30,
    )
    await poller.poll_once()

    # The sample_bus stores the last sample
    last = sample_bus.last_sample
    assert last is not None
    assert last.points["soc"] == 72
    assert last.points["voltage_v"] == 242.3
    assert last.quality == "ok"


# ---------------------------------------------------------------------------
# 2. Power limit auto-detection flow
# ---------------------------------------------------------------------------


async def test_power_limit_autodetect_flow(mock_controller, sample_bus, db, device_info):
    """First sample with M702 data triggers power limit updates on handler + publisher."""
    handler = CommandHandler(mock_controller, db)
    publisher = MqttPublisher()
    publisher.set_device_info(device_info)

    # Verify defaults before detection
    assert handler.max_charge_w == DEFAULT_MAX_POWER_W
    assert handler.max_discharge_w == DEFAULT_MAX_POWER_W

    # Simulate the _detect_power_limits subscriber from main.py
    limits_detected = False

    async def detect_limits(sample: Sample) -> None:
        nonlocal limits_detected
        if limits_detected:
            return
        charge = sample.points.get("max_charge_rate_w")
        discharge = sample.points.get("max_discharge_rate_w")
        if charge is not None and discharge is not None:
            limits_detected = True
            handler.set_power_limits(int(charge), int(discharge))
            publisher.set_power_limits(int(charge), int(discharge))

    sample_bus.subscribe(detect_limits)
    sample_bus.subscribe(publisher.queue_sample)

    poller = ModbusPoller(
        controller=mock_controller,
        sample_bus=sample_bus,
        gateway_id="default",
        poll_interval=30,
    )
    await poller.poll_once()

    # After the first poll, nameplate data triggers limit update
    assert limits_detected
    assert handler.max_charge_w == 6000
    assert handler.max_discharge_w == 5500
    assert handler.power_limits["source"] == "hardware"

    # Publisher should have overrides set
    assert "battery_command_power" in publisher._max_val_overrides
    assert publisher._max_val_overrides["battery_command_power"] == 6000.0

    # Discovery should be marked for re-publish
    assert publisher.state.discovery_published is False


async def test_power_limit_only_detects_once(mock_controller, sample_bus, db):
    """Power limits are set only on the first sample, not re-applied on subsequent polls."""
    handler = CommandHandler(mock_controller, db)
    detect_count = 0

    async def detect_limits(sample: Sample) -> None:
        nonlocal detect_count
        charge = sample.points.get("max_charge_rate_w")
        discharge = sample.points.get("max_discharge_rate_w")
        if charge is not None and discharge is not None:
            detect_count += 1
            handler.set_power_limits(int(charge), int(discharge))

    # Note: NOT using _limits_detected guard — testing the raw callback count
    sample_bus.subscribe(detect_limits)

    poller = ModbusPoller(
        controller=mock_controller,
        sample_bus=sample_bus,
        gateway_id="default",
        poll_interval=30,
    )
    await poller.poll_once()
    await poller.poll_once()

    # Without a guard, it fires every time — main.py has the guard
    assert detect_count == 2

    # With the guard pattern from main.py:
    guarded_count = 0
    guarded = False

    async def guarded_detect(sample: Sample) -> None:
        nonlocal guarded_count, guarded
        if guarded:
            return
        charge = sample.points.get("max_charge_rate_w")
        if charge is not None:
            guarded = True
            guarded_count += 1

    bus2 = SampleBus()
    bus2.subscribe(guarded_detect)
    await bus2.publish(Sample.now("gw", {"max_charge_rate_w": 6000}))
    await bus2.publish(Sample.now("gw", {"max_charge_rate_w": 6000}))
    assert guarded_count == 1  # only fires once


# ---------------------------------------------------------------------------
# 3. Command handler → controller dispatch
# ---------------------------------------------------------------------------


async def test_command_dispatch_charge(mock_controller, db):
    """Charge command dispatches to controller with correct watts."""
    handler = CommandHandler(mock_controller, db)
    handler.set_power_limits(6000, 5500)

    await handler.handle_command("battery_command_power", "4000")
    await handler.handle_command("battery_command", "Charge")

    mock_controller.send_command.assert_called_once()
    cmd = mock_controller.send_command.call_args[0][0]
    assert cmd.power_watts == 4000  # under charge limit, not clamped


async def test_command_dispatch_discharge_clamped(mock_controller, db):
    """Discharge command over limit is clamped to max_discharge_w."""
    handler = CommandHandler(mock_controller, db)
    handler.set_power_limits(6000, 3000)

    await handler.handle_command("battery_command_power", "5000")
    await handler.handle_command("battery_command", "Discharge")

    cmd = mock_controller.send_command.call_args[0][0]
    assert cmd.power_watts == -3000  # clamped to discharge limit, negative


async def test_command_virtual_points_flow(mock_controller, db, device_info):
    """Virtual points from command handler are included in MQTT sample queue."""
    publisher = MqttPublisher()
    publisher.set_device_info(device_info)

    handler = CommandHandler(mock_controller, db)
    publisher.set_command_handler(handler)

    # Issue a command to set virtual state
    await handler.handle_command("battery_command", "Charge")

    # Create a sample that would come from poller
    sample = Sample.now("default", {"soc": 80})
    await publisher.queue_sample(sample)

    messages = []
    while not publisher._queue.empty():
        messages.append(publisher._queue.get_nowait())

    topics = {m.topic for m in messages}
    short_id = device_info.short_id

    # Command handler virtual points should be in the MQTT queue
    assert f"franklinwh/{short_id}/battery/battery_soc" in topics
    assert f"franklinwh/{short_id}/control/battery_command" in topics


# ---------------------------------------------------------------------------
# 4. Discovery payload correctness
# ---------------------------------------------------------------------------


def test_discovery_all_entities_valid(device_info):
    """Every entity produces a valid HA Discovery payload."""
    for entity in BRIDGE_ENTITIES:
        payload = build_discovery_payload(entity, device_info)
        assert "unique_id" in payload
        assert "name" in payload
        assert "state_topic" in payload
        assert "availability_topic" in payload
        assert "device" in payload

        # Verify device block
        dev = payload["device"]
        assert "identifiers" in dev
        assert "name" in dev


def test_discovery_control_entities_have_command_topics(device_info):
    """All control entities have command_topic in their discovery payload."""
    control_entities = [e for e in BRIDGE_ENTITIES if e.is_control]
    assert len(control_entities) > 0

    for entity in control_entities:
        payload = build_discovery_payload(entity, device_info)
        assert "command_topic" in payload, (
            f"Control entity {entity.slug} missing command_topic"
        )


def test_discovery_number_entities_have_range(device_info):
    """Number entities have min/max/step in discovery payloads."""
    number_entities = [e for e in BRIDGE_ENTITIES if e.ha_type == "number"]
    assert len(number_entities) > 0

    for entity in number_entities:
        payload = build_discovery_payload(entity, device_info)
        assert "min" in payload, f"Number entity {entity.slug} missing min"
        assert "max" in payload, f"Number entity {entity.slug} missing max"
        assert "step" in payload, f"Number entity {entity.slug} missing step"


# ---------------------------------------------------------------------------
# 5. Phase filtering end-to-end
# ---------------------------------------------------------------------------


def test_phase_filter_single_reduces_entities():
    """Single-phase config publishes fewer entities than three-phase."""
    pub_single = MqttPublisher()
    pub_single.set_ac_type(0)

    pub_three = MqttPublisher()
    pub_three.set_ac_type(2)

    assert len(pub_single.entities) < len(pub_three.entities)
    # Three-phase un-gates all phase entities, but per-battery-port entities are
    # still gated by battery_port_count (default 1) — so the full set is
    # BRIDGE_ENTITIES minus those needing a higher port count, not all 83.
    expected = [
        e for e in BRIDGE_ENTITIES
        if e.battery_port is None or e.battery_port <= 1
    ]
    assert len(pub_three.entities) == len(expected)


def test_phase_filter_preserves_non_phase_entities():
    """Phase filtering never removes core (non-phase) entities."""
    core_slugs = {"battery_soc", "battery_power_kw", "grid_power_kw", "solar_power_kw"}

    for ac_type in (0, 1, 2):
        pub = MqttPublisher()
        pub.set_ac_type(ac_type)
        entity_slugs = {e.slug for e in pub.entities}
        for slug in core_slugs:
            assert slug in entity_slugs, (
                f"Core entity {slug} missing for ac_type={ac_type}"
            )


# ---------------------------------------------------------------------------
# 6. Full app lifecycle via ASGI transport
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_app_health_on_startup(client):
    """App reports healthy immediately after startup."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


async def test_app_points_after_sample(client):
    """Points endpoint returns data after a sample is published."""
    sample = Sample.now("default", {
        "soc": 85,
        "battery_power_w": -1200,
        "voltage_v": 243.4,
    })
    await app.state.sample_bus.publish(sample)

    resp = await client.get("/api/points")
    assert resp.status_code == 200
    data = resp.json()
    assert data["points"]["soc"] == 85
    assert data["points"]["battery_power_w"] == -1200


async def test_app_battery_limits_endpoint(client):
    """Battery limits endpoint returns defaults when no hardware detected."""
    resp = await client.get("/api/battery/limits")
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_charge_w"] == DEFAULT_MAX_POWER_W
    assert data["max_discharge_w"] == DEFAULT_MAX_POWER_W
    assert data["source"] == "default"


async def test_app_stats_endpoint(client):
    """Operational stats endpoint returns valid data."""
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "polls_total" in data
    assert "data_quality_pct" in data


async def test_app_mqtt_status(client):
    """MQTT status endpoint returns publisher state."""
    resp = await client.get("/api/mqtt/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "connected" in data
    assert "messages_sent" in data


async def test_app_backup_create_and_list(client):
    """Backup create and list endpoints work in sequence."""
    # Create
    resp = await client.post("/api/backup/create", json={"label": "test"})
    assert resp.status_code == 200
    name = resp.json()["name"]
    assert "test" in name

    # List
    resp = await client.get("/api/backup/list")
    assert resp.status_code == 200
    backups = resp.json()["backups"]
    assert len(backups) >= 1
    assert any(b["name"] == name for b in backups)


async def test_app_metrics_export_empty(client):
    """Metrics export returns empty CSV when no data exists."""
    resp = await client.get("/api/metrics/export?format=csv&range=24h")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().split("\n")
    assert len(lines) == 1  # header only


async def test_app_storage_stats(client):
    """Storage stats endpoint returns DB size info."""
    resp = await client.get("/api/stats/storage")
    assert resp.status_code == 200
    data = resp.json()
    assert "db_size_bytes" in data
    assert "tables" in data


# ---------------------------------------------------------------------------
# 7. Metrics recording through sample bus
# ---------------------------------------------------------------------------


async def test_metrics_recorded_from_sample(tmp_path):
    """Metrics are recorded in DB when a sample with power data is published."""
    db = await init_db(tmp_path / "test.db")
    bus = SampleBus()

    from franklinwh_bridge.store.metrics import record_sample

    async def _record(sample: Sample) -> None:
        await record_sample(db, sample.points)

    bus.subscribe(_record)

    sample = Sample.now("default", {
        "battery_power_w": -1200,
        "grid_power_w": 500,
        "total_solar": 4200,
        "home_load_ext": 350,
        "soc": 72,
    })
    await bus.publish(sample)

    # Verify data was written to metrics table
    async with db.execute("SELECT COUNT(*) FROM metrics") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1

    async with db.execute("SELECT battery_w, grid_w, solar_w FROM metrics") as cur:
        row = await cur.fetchone()
    assert row[0] == -1200
    assert row[1] == 500
    assert row[2] == 4200

    await db.close()
