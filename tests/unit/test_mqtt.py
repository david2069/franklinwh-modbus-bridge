"""Tests for MQTT publisher — EntityDef registry, discovery payloads, and queue."""

import asyncio

import pytest

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES, get_entity_by_slug
from franklinwh_bridge.publish.mqtt_publisher import (
    DeviceInfo,
    MqttPublisher,
    build_discovery_payload,
)


@pytest.fixture
def device_info():
    return DeviceInfo(
        serial="10060006A02F00000001",
        model="aGate X",
        firmware="V10R01B04D00",
        name="FHP",
    )


# --- Entity registry ---

def test_entity_registry_not_empty():
    assert len(BRIDGE_ENTITIES) > 20


def test_entity_slugs_unique():
    slugs = [e.slug for e in BRIDGE_ENTITIES]
    assert len(slugs) == len(set(slugs))


def test_core_entities_present():
    slugs = {e.slug for e in BRIDGE_ENTITIES}
    assert "battery_soc" in slugs
    assert "battery_power_kw" in slugs
    assert "grid_power_kw" in slugs
    assert "grid_frequency_hz" in slugs
    assert "solar_power_kw" in slugs
    assert "home_load_kw" in slugs
    assert "operating_mode" in slugs
    assert "operating_mode_sensor" in slugs


def test_get_entity_by_slug():
    ent = get_entity_by_slug("battery_soc")
    assert ent is not None
    assert ent.ha_type == "sensor"
    assert ent.device_class == "battery"
    assert ent.unit == "%"
    assert get_entity_by_slug("nonexistent") is None


def test_format_value_scaling():
    ent = get_entity_by_slug("battery_power_kw")
    assert ent.format_value(1500) == "1.500"
    assert ent.format_value(-1200) == "-1.200"
    assert ent.format_value(0) == "0.000"
    assert ent.format_value(509) == "0.509"

    soc = get_entity_by_slug("battery_soc")
    assert soc.format_value(85) == "85"

    state = get_entity_by_slug("battery_state")
    assert state.format_value("Charging") == "Charging"


def test_entity_topic_methods():
    ent = get_entity_by_slug("battery_soc")
    assert ent.state_topic("00000001") == "franklinwh/00000001/battery/battery_soc"
    assert ent.discovery_topic("00000001") == (
        "homeassistant/sensor/franklinwh_00000001_battery_soc/config"
    )
    assert ent.unique_id("00000001") == "franklinwh_00000001_battery_soc"
    assert ent.command_topic("00000001") is None


def test_control_entity_has_command_topic():
    ent = get_entity_by_slug("operating_mode")
    assert ent is not None
    assert ent.is_control is True
    assert ent.ha_type == "select"
    assert ent.command_topic("00000001") == (
        "franklinwh/00000001/control/operating_mode/set"
    )
    assert len(ent.options) > 0


def test_number_entity_has_limits():
    ent = get_entity_by_slug("self_reserve_pct")
    assert ent is not None
    assert ent.ha_type == "number"
    assert ent.min_val == 0
    assert ent.max_val == 100
    assert ent.step == 1


# --- Discovery payloads ---

def test_build_discovery_payload_sensor(device_info):
    ent = get_entity_by_slug("battery_soc")
    payload = build_discovery_payload(ent, device_info)

    assert payload["name"] == "State of Charge"
    assert payload["unique_id"] == "franklinwh_00000001_battery_soc"
    assert payload["state_topic"] == "franklinwh/00000001/battery/battery_soc"
    assert payload["availability_topic"] == "franklinwh/00000001/availability"
    assert payload["device_class"] == "battery"
    assert payload["unit_of_measurement"] == "%"
    assert payload["state_class"] == "measurement"
    assert payload["icon"] == "mdi:battery"

    device = payload["device"]
    assert device["manufacturer"] == "FranklinWH Technologies Co., Ltd"
    assert device["model"] == "aGate X"
    assert device["serial_number"] == "10060006A02F00000001"
    assert "franklinwh_10060006A02F00000001" in device["identifiers"]


def test_build_discovery_payload_kw_sensor(device_info):
    ent = get_entity_by_slug("battery_power_kw")
    payload = build_discovery_payload(ent, device_info)
    assert payload["unit_of_measurement"] == "kW"
    assert payload["device_class"] == "power"


def test_build_discovery_payload_no_device_class(device_info):
    ent = get_entity_by_slug("battery_state")
    payload = build_discovery_payload(ent, device_info)
    assert "device_class" not in payload
    assert "unit_of_measurement" not in payload
    assert "state_class" not in payload


def test_build_discovery_payload_select(device_info):
    ent = get_entity_by_slug("operating_mode")
    payload = build_discovery_payload(ent, device_info)
    assert payload["command_topic"] == "franklinwh/00000001/control/operating_mode/set"
    assert "options" in payload
    assert isinstance(payload["options"], list)


def test_build_discovery_payload_number(device_info):
    ent = get_entity_by_slug("self_reserve_pct")
    payload = build_discovery_payload(ent, device_info)
    assert payload["min"] == 0
    assert payload["max"] == 100
    assert payload["step"] == 1
    assert payload["command_topic"] == "franklinwh/00000001/control/self_reserve_pct/set"


# --- Device info ---

def test_device_info_short_id(device_info):
    assert device_info.short_id == "00000001"


def test_device_info_ha_block(device_info):
    block = device_info.ha_device_block(app_version="0.1.0")
    assert block["name"] == "FHP"
    assert "V10R01B04D00" in block["sw_version"]
    assert "0.1.0" in block["sw_version"]


def test_duplicate_serial_does_not_collide():
    """Two gateways reporting the same serial must get distinct MQTT identity,
    so Home Assistant Discovery does not merge them into one device."""
    serial = "10060006A02F00000001"
    default = DeviceInfo(serial=serial, gateway_id="default")
    second = DeviceInfo(serial=serial, gateway_id="gateway_2")

    # Default keeps the historic serial-only namespace (backward compatible).
    assert default.short_id == "00000001"
    assert default.ha_device_block()["identifiers"] == [f"franklinwh_{serial}"]

    # The additional gateway is namespaced by gateway_id → no collision.
    # The FORM changed (2026-09-13) from "{gateway_id}_{serial_tail}" to the
    # slugified gateway_id alone: the serial tail is a blind last-8 slice, which
    # turned a mock's "MOCK-MOCK GW 1" into "OCK GW 1" and put spaces in the
    # topic. What this test guards is distinctness, not the exact string.
    assert second.short_id == "gateway_2"
    assert default.short_id != second.short_id
    assert " " not in second.short_id
    assert (
        default.ha_device_block()["identifiers"]
        != second.ha_device_block()["identifiers"]
    )

    # ... and the per-entity unique_id / discovery topic differ too.
    ent = get_entity_by_slug("battery_soc")
    assert (
        build_discovery_payload(ent, default)["unique_id"]
        != build_discovery_payload(ent, second)["unique_id"]
    )


# --- Queue and publish ---

async def test_queue_sample_per_entity():
    publisher = MqttPublisher(gateway_id="gw1")
    publisher.set_device_info(DeviceInfo(serial="10060006A02F00000001"))

    sample = Sample.now("gw1", {"soc": 85, "battery_power_w": -1200, "voltage_v": 243.4})
    await publisher.queue_sample(sample)

    queued = []
    while not publisher._queue.empty():
        queued.append(publisher._queue.get_nowait())

    topics = {m.topic for m in queued}
    assert "franklinwh/00000001/battery/battery_soc" in topics
    assert "franklinwh/00000001/battery/battery_power_kw" in topics
    assert "franklinwh/00000001/status/grid_voltage_v" in topics

    soc_msg = next(m for m in queued if "battery_soc" in m.topic)
    assert soc_msg.payload == "85"
    assert soc_msg.retain is True

    power_msg = next(m for m in queued if "battery_power_kw" in m.topic)
    assert power_msg.payload == "-1.200"


async def test_queue_skips_missing_keys():
    publisher = MqttPublisher(gateway_id="gw1")
    publisher.set_device_info(DeviceInfo(serial="10060006A02F00000001"))

    sample = Sample.now("gw1", {"soc": 85})
    await publisher.queue_sample(sample)

    queued = []
    while not publisher._queue.empty():
        queued.append(publisher._queue.get_nowait())

    topics = {m.topic for m in queued}
    assert "franklinwh/00000001/battery/battery_soc" in topics
    assert "franklinwh/00000001/battery/battery_power_kw" not in topics


async def test_queue_requires_device_info():
    publisher = MqttPublisher(gateway_id="gw1")
    sample = Sample.now("gw1", {"soc": 85})
    await publisher.queue_sample(sample)
    assert publisher._queue.qsize() == 0


async def test_queue_full_drops():
    publisher = MqttPublisher(gateway_id="gw1")
    publisher.set_device_info(DeviceInfo(serial="10060006A02F00000001"))
    publisher._queue = asyncio.Queue(maxsize=1)

    sample = Sample.now("gw1", {
        "soc": 85, "battery_power_w": 100, "voltage_v": 240,
        "frequency_hz": 50, "current_a": 3.6,
    })
    await publisher.queue_sample(sample)
    assert publisher._queue.qsize() == 1


# --- State and config ---

def test_mqtt_state_defaults():
    publisher = MqttPublisher()
    assert publisher.state.connected is False
    assert publisher.state.messages_sent == 0
    assert publisher.state.discovery_published is False


def test_set_device_info_resets_discovery():
    publisher = MqttPublisher()
    publisher._state.discovery_published = True
    publisher.set_device_info(DeviceInfo(serial="ABC123"))
    assert publisher.state.discovery_published is False
    assert publisher.device_info.serial == "ABC123"


def test_backoff_delay():
    publisher = MqttPublisher()
    assert publisher._backoff_delay(0) == 5.0
    assert publisher._backoff_delay(3) == 40.0
    assert publisher._backoff_delay(10) == 60.0


# --- Per-phase entity definitions ---

def test_per_phase_entities_exist():
    """Verify all 21 per-phase entities are defined."""
    phase_entities = [e for e in BRIDGE_ENTITIES if e.phase is not None]
    assert len(phase_entities) == 21

    # L1 (phase=1): 6 entities
    l1 = [e for e in phase_entities if e.phase == 1]
    assert len(l1) == 6

    # L2 (phase=2): 7 entities (6 + VL1L2)
    l2 = [e for e in phase_entities if e.phase == 2]
    assert len(l2) == 7

    # L3 (phase=3): 8 entities (6 + VL2L3 + VL3L1)
    l3 = [e for e in phase_entities if e.phase == 3]
    assert len(l3) == 8


def test_per_phase_entity_slugs():
    """Verify L1 per-phase entity slugs."""
    slugs = {e.slug for e in BRIDGE_ENTITIES if e.phase == 1}
    assert "grid_voltage_l1_v" in slugs
    assert "grid_current_l1_a" in slugs
    assert "grid_power_l1_w" in slugs
    assert "grid_pf_l1" in slugs
    assert "grid_va_l1" in slugs
    assert "grid_var_l1" in slugs


def test_total_entity_count():
    """Verify total entity count including per-phase + target SoC."""
    assert len(BRIDGE_ENTITIES) == 83  # +1: Solar Energy Proximal (502.OutWh)


# --- Phase filtering in MQTT publisher ---

def test_set_ac_type_single_phase():
    """Single-phase (ac_type=0): publishes phase=None + phase=1."""
    publisher = MqttPublisher()
    publisher.set_ac_type(0)

    phases_present = {e.phase for e in publisher.entities}
    assert None in phases_present
    assert 1 in phases_present
    assert 2 not in phases_present
    assert 3 not in phases_present

    phase_count = sum(1 for e in publisher.entities if e.phase is not None)
    assert phase_count == 6  # 6 L1 entities


def test_set_ac_type_split_phase():
    """Split-phase (ac_type=1): publishes phase=None + phase=1 + phase=2."""
    publisher = MqttPublisher()
    publisher.set_ac_type(1)

    phases_present = {e.phase for e in publisher.entities}
    assert None in phases_present
    assert 1 in phases_present
    assert 2 in phases_present
    assert 3 not in phases_present

    phase_count = sum(1 for e in publisher.entities if e.phase is not None)
    assert phase_count == 13  # 6 L1 + 7 L2


def test_set_ac_type_three_phase():
    """Three-phase (ac_type=2): publishes all entities."""
    publisher = MqttPublisher()
    publisher.set_ac_type(2)
    publisher.set_battery_port_count(3)

    phases_present = {e.phase for e in publisher.entities}
    assert None in phases_present
    assert 1 in phases_present
    assert 2 in phases_present
    assert 3 in phases_present

    ports_present = {e.battery_port for e in publisher.entities}
    assert 1 in ports_present
    assert 2 in ports_present
    assert 3 in ports_present

    assert len(publisher.entities) == len(BRIDGE_ENTITIES)


def test_set_ac_type_resets_discovery():
    """Setting AC type should reset discovery_published flag."""
    publisher = MqttPublisher()
    publisher._state.discovery_published = True
    publisher.set_ac_type(0)
    assert publisher.state.discovery_published is False


def test_set_ac_type_tracks_removed_entities():
    """Phase filtering should track removed entities for tombstoning."""
    publisher = MqttPublisher()
    publisher.set_ac_type(0)  # Single phase

    # Should have removed L2 and L3 entities
    removed_slugs = {e.slug for e in publisher._removed_entities}
    assert "grid_voltage_l2_v" in removed_slugs
    assert "grid_voltage_l3_v" in removed_slugs
    assert "grid_voltage_l1l2_v" in removed_slugs
    assert "grid_voltage_l2l3_v" in removed_slugs

    # L1 entities should NOT be in removed list
    assert "grid_voltage_l1_v" not in removed_slugs
    # Non-phase entities should NOT be in removed list
    assert "battery_soc" not in removed_slugs

    # Total removed = 21 (7 L2 + 8 L3 phase + 6 battery port 2+3)
    assert len(publisher._removed_entities) == 21


def test_set_ac_type_split_tracks_removed():
    """Split phase should only remove L3 entities."""
    publisher = MqttPublisher()
    publisher.set_ac_type(1)  # Split phase

    removed_slugs = {e.slug for e in publisher._removed_entities}
    assert "grid_voltage_l2_v" not in removed_slugs  # L2 kept
    assert "grid_voltage_l3_v" in removed_slugs  # L3 removed
    assert len(publisher._removed_entities) == 14  # 8 L3 + 6 battery port 2+3


def test_set_ac_type_three_phase_no_removed():
    """Three-phase should only remove battery port 2+3 (default NPrt=1)."""
    publisher = MqttPublisher()
    publisher.set_ac_type(2)
    assert len(publisher._removed_entities) == 6  # battery port 2+3 only


def test_entity_source_annotations():
    """Every entity should have a source annotation."""
    for entity in BRIDGE_ENTITIES:
        assert entity.source, f"Entity {entity.slug} missing source annotation"


def test_entity_source_format():
    """Source annotations follow model.point or ext.addr or virtual format."""
    import re
    valid_pattern = re.compile(
        r"^(\d{3}\.\w+|ext\.\d{5}|virtual)$"
    )
    for entity in BRIDGE_ENTITIES:
        assert valid_pattern.match(entity.source), (
            f"Entity {entity.slug} has invalid source format: {entity.source!r}"
        )


def test_pv_energy_proximal_entity_uses_502_outwh():
    """Proximal solar energy comes from the standard SunSpec 502.OutWh
    (library-read), not the raw-pymodbus 15512 derivation."""
    ent = get_entity_by_slug("pv_energy_proximal_kwh")
    assert ent is not None
    assert ent.stat_key == "502.OutWh"
    assert ent.source == "502.OutWh"
    assert ent.unit == "kWh"
    assert ent.device_class == "energy"
    assert ent.value_scale == 0.001
