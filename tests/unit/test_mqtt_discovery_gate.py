"""HA Discovery must not depend on the DEFAULT gateway having device info.

With the default gateway unconfigured (or offline) and a mock added to explore
the bridge, discovery was never published — the gate checked only the default
device — so the mock's samples went to topics HA had no entities for.
"""

from __future__ import annotations

import time

from franklinwh_bridge.publish import mqtt_publisher as mp
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher


class _RecordingClient:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish(self, topic, payload=None, retain=False, qos=0):
        self.topics.append(topic)


def test_nothing_to_discover_without_any_device():
    pub = MqttPublisher(host="x")
    assert pub._has_discoverable() is False


async def test_registered_gateway_is_discoverable_without_default():
    pub = MqttPublisher(host="x")
    assert pub._device_info is None  # default gateway unconfigured
    pub.register_device("demo", DeviceInfo(serial="MOCK-demo", gateway_id="demo"))
    assert pub._has_discoverable() is True

    client = _RecordingClient()
    await pub._publish_discovery(client)
    assert any(t.startswith("homeassistant/") for t in client.topics)
    assert pub.get_device("demo").discovery_published


def test_unregistered_warning_waits_for_the_grace_period(monkeypatch, caplog):
    pub = MqttPublisher(host="x")
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    pub._warn_unregistered("demo")  # first sample, just before registration
    assert "no registered MQTT device" not in caplog.text

    clock["t"] += mp._UNREGISTERED_GRACE_S + 1
    pub._warn_unregistered("demo")  # still unregistered — that's a real problem
    assert "no registered MQTT device" in caplog.text


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def handle_command(self, slug: str, payload: str) -> None:
        self.calls.append((slug, payload))


class _Msg:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


async def test_commands_route_to_the_gateway_their_topic_names():
    """A command for a second gateway used to go to the DEFAULT gateway."""
    pub = MqttPublisher(host="x")
    default_h, demo_h = _Handler(), _Handler()
    pub.set_device_info(DeviceInfo(serial="AG0001"))
    pub.set_command_handler(default_h)
    pub.register_device("demo", DeviceInfo(serial="MOCK-DEMO", gateway_id="demo"),
                        command_handler=demo_h)

    sids = pub._command_targets()
    default_sid = pub._device_info.short_id
    demo_sid = pub.get_device("demo").device_info.short_id
    assert set(sids) == {default_sid, demo_sid}

    await pub._handle_mqtt_message(
        _Msg(f"franklinwh/{demo_sid}/control/operating_mode/set", b"TOU"))
    await pub._handle_mqtt_message(
        _Msg(f"franklinwh/{default_sid}/control/battery_command/set", b"Release"))
    await pub._handle_mqtt_message(
        _Msg("franklinwh/someone_else/control/battery_command/set", b"Force Charge"))

    assert demo_h.calls == [("operating_mode", "TOU")]
    assert default_h.calls == [("battery_command", "Release")]


async def test_registered_gateway_is_commandable_without_a_default():
    pub = MqttPublisher(host="x")
    demo_h = _Handler()
    pub.register_device("demo", DeviceInfo(serial="MOCK-DEMO", gateway_id="demo"),
                        command_handler=demo_h)
    sid = pub.get_device("demo").device_info.short_id
    await pub._handle_mqtt_message(_Msg(f"franklinwh/{sid}/control/tou_reserve_pct/set", b"40"))
    assert demo_h.calls == [("tou_reserve_pct", "40")]
