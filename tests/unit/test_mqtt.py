"""Tests for MQTT publisher — entity mapping, discovery payloads, and queue."""

import json

import pytest

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.publish.mqtt_publisher import (
    EntityMapping,
    MqttPublisher,
    build_discovery_payload,
    build_discovery_topic,
    build_entity_mappings,
)


@pytest.fixture
def sample_catalog():
    return [
        {"point_name": "soc", "type": "uint16", "unit": "%", "model_id": 124},
        {"point_name": "power", "type": "int16", "unit": "W", "model_id": 124},
        {"point_name": "grid_voltage", "type": "float", "unit": "V", "model_id": 101},
        {"point_name": "mode", "type": "enum16", "unit": None, "model_id": 64113},
        {"point_name": "energy_total", "type": "uint32", "unit": "Wh", "model_id": 101},
    ]


def test_build_entity_mappings(sample_catalog):
    mappings = build_entity_mappings(sample_catalog, "gw1")
    assert len(mappings) == 5

    soc = next(m for m in mappings if m.point_name == "soc")
    assert soc.ha_component == "sensor"
    assert soc.device_class == "battery"
    assert soc.unit == "%"
    assert soc.unique_id == "franklinwh_gw1_soc"

    power = next(m for m in mappings if m.point_name == "power")
    assert power.device_class == "power"

    mode = next(m for m in mappings if m.point_name == "mode")
    assert mode.ha_component == "sensor"
    assert mode.device_class is None


def test_build_discovery_payload():
    mapping = EntityMapping(
        point_name="soc",
        ha_component="sensor",
        device_class="battery",
        unit="%",
        unique_id="franklinwh_gw1_soc",
        name="FranklinWH Soc",
    )
    payload = build_discovery_payload(mapping, "gw1")

    assert payload["name"] == "FranklinWH Soc"
    assert payload["unique_id"] == "franklinwh_gw1_soc"
    assert payload["state_topic"] == "franklinwh/gw1/state"
    assert payload["availability_topic"] == "franklinwh/gw1/availability"
    assert payload["device_class"] == "battery"
    assert payload["unit_of_measurement"] == "%"
    assert payload["state_class"] == "measurement"
    assert "value_json.soc" in payload["value_template"]
    assert payload["device"]["manufacturer"] == "FranklinWH"
    assert payload["device"]["model"] == "aGate"


def test_build_discovery_payload_no_device_class():
    mapping = EntityMapping(
        point_name="mode",
        ha_component="sensor",
        device_class=None,
        unit=None,
        unique_id="franklinwh_gw1_mode",
        name="FranklinWH Mode",
    )
    payload = build_discovery_payload(mapping, "gw1")
    assert "device_class" not in payload
    assert "unit_of_measurement" not in payload
    assert "state_class" not in payload


def test_build_discovery_topic():
    mapping = EntityMapping(
        point_name="soc",
        ha_component="sensor",
        device_class="battery",
        unit="%",
        unique_id="franklinwh_gw1_soc",
        name="FranklinWH Soc",
    )
    topic = build_discovery_topic(mapping)
    assert topic == "homeassistant/sensor/franklinwh_gw1_soc/config"


async def test_queue_sample():
    publisher = MqttPublisher(gateway_id="gw1")
    sample = Sample.now("gw1", {"soc": 85, "power": -1200})
    await publisher.queue_sample(sample)
    assert publisher._queue.qsize() == 1
    msg = publisher._queue.get_nowait()
    assert msg.topic == "franklinwh/gw1/state"
    payload = json.loads(msg.payload)
    assert payload["soc"] == 85
    assert msg.retain is True


async def test_queue_full_drops():
    publisher = MqttPublisher(gateway_id="gw1")
    publisher._queue = __import__("asyncio").Queue(maxsize=2)
    await publisher.queue_sample(Sample.now("gw1", {"a": 1}))
    await publisher.queue_sample(Sample.now("gw1", {"a": 2}))
    await publisher.queue_sample(Sample.now("gw1", {"a": 3}))
    assert publisher._queue.qsize() == 2


def test_mqtt_state_defaults():
    publisher = MqttPublisher()
    assert publisher.state.connected is False
    assert publisher.state.messages_sent == 0
    assert publisher.state.discovery_published is False


def test_set_entity_mappings():
    publisher = MqttPublisher()
    publisher._state.discovery_published = True
    mappings = [
        EntityMapping("soc", "sensor", "battery", "%", "uid1", "SOC"),
    ]
    publisher.set_entity_mappings(mappings)
    assert len(publisher.entity_mappings) == 1
    assert publisher.state.discovery_published is False


def test_backoff_delay():
    publisher = MqttPublisher()
    assert publisher._backoff_delay(0) == 5.0
    assert publisher._backoff_delay(3) == 40.0
    assert publisher._backoff_delay(10) == 60.0
