"""The stored MQTT namespace is used everywhere (#90); PATCH can't change it (#87).

Topic Prefix and Discovery Prefix were saved and loaded but ignored: every topic,
unique_id and device identifier was hard-coded ``franklinwh`` / ``homeassistant``.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.publish.entities import get_entity_by_slug
from franklinwh_bridge.publish.mqtt_publisher import (
    DeviceInfo,
    MqttPublisher,
    build_discovery_payload,
)

SERIAL = "10060006A02F00000001"


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def handle_command(self, slug: str, payload: str, requester: str = "") -> None:
        self.calls.append((slug, payload))


class _Msg:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self) -> None:
        self.published: dict[str, str | bytes] = {}

    async def publish(self, topic, payload, retain=False, qos=0) -> None:
        self.published[topic] = payload


def _custom() -> MqttPublisher:
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb", discovery_prefix="ha_disc")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    return pub


def test_default_output_is_unchanged():
    ent = get_entity_by_slug("battery_soc")
    payload = build_discovery_payload(ent, DeviceInfo(serial=SERIAL))
    assert payload["unique_id"] == "franklinwh_00000001_battery_soc"
    assert payload["availability_topic"] == "franklinwh/00000001/availability"
    assert payload["device"]["identifiers"] == [f"franklinwh_modbus_{SERIAL}"]
    assert ent.discovery_topic("00000001") == (
        "homeassistant/sensor/franklinwh_00000001_battery_soc/config"
    )


async def test_custom_namespace_reaches_discovery_and_availability():
    pub = _custom()
    client = _Client()
    ent = get_entity_by_slug("battery_soc")
    await pub._publish_device_discovery(client, pub._device_info, [ent], None, {}, "1.0")
    await pub._publish_availability(client, online=True)

    topic = "ha_disc/sensor/fwh_mb_00000001_battery_soc/config"
    assert set(client.published) == {topic, "fwh_mb/00000001/availability"}
    payload = json.loads(client.published[topic])
    assert payload["unique_id"] == "fwh_mb_00000001_battery_soc"
    assert payload["state_topic"] == "fwh_mb/00000001/battery/battery_soc"
    assert payload["availability_topic"] == "fwh_mb/00000001/availability"
    assert payload["device"]["identifiers"] == [f"fwh_mb_modbus_{SERIAL}"]
    assert not any("franklinwh" in t or "homeassistant" in t for t in client.published)


def test_custom_namespace_reaches_command_topics():
    ent = get_entity_by_slug("operating_mode")
    payload = build_discovery_payload(ent, DeviceInfo(serial=SERIAL), prefix="fwh_mb")
    assert payload["command_topic"] == "fwh_mb/00000001/control/operating_mode/set"


async def test_commands_outside_our_namespace_are_ignored():
    """Another producer's control topic is not ours to obey."""
    pub = _custom()
    handler = _Handler()
    pub.set_command_handler(handler)
    sid = pub._device_info.short_id
    await pub._handle_mqtt_message(
        _Msg(f"franklinwh/{sid}/control/battery_command/set", b"Force Charge")
    )
    await pub._handle_mqtt_message(_Msg(f"fwh_mb/{sid}/control/battery_command/set", b"Release"))
    assert handler.calls == [("battery_command", "Release")]


def test_from_db_config_uses_stored_prefixes():
    pub = MqttPublisher.from_db_config({"topic_prefix": "fwh_mb", "discovery_prefix": "ha_disc"})
    assert (pub.topic_prefix, pub.discovery_prefix) == ("fwh_mb", "ha_disc")
    blank = MqttPublisher.from_db_config({"topic_prefix": "", "discovery_prefix": None})
    assert (blank.topic_prefix, blank.discovery_prefix) == ("franklinwh", "homeassistant")


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


async def test_patch_refuses_a_namespace_change(client):
    for body in ({"topic_prefix": "fwh_home"}, {"discovery_prefix": "other"}):
        resp = await client.patch("/api/mqtt/config", json=body)
        assert resp.status_code == 409, body
        assert "#87" in resp.json()["detail"]
    cfg = (await client.get("/api/mqtt/config")).json()
    assert (cfg["topic_prefix"], cfg["discovery_prefix"]) == ("fwh_mb", "homeassistant")


async def test_patch_accepts_the_current_namespace(client):
    """The Settings form always re-sends both prefixes with any other edit."""
    resp = await client.patch(
        "/api/mqtt/config",
        json={
            "host": "broker.local",
            "topic_prefix": "fwh_mb",
            "discovery_prefix": "homeassistant",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["host"] == "broker.local"


@pytest.mark.parametrize("bad", ["fwh/mb", "fwh+", "fwh#", "has space", "X", ""])
async def test_patch_rejects_unsafe_topic_prefix(client, bad):
    resp = await client.patch("/api/mqtt/config", json={"topic_prefix": bad})
    assert resp.status_code == 422
