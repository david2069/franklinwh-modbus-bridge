"""MQTT commands stop when the Control publishing group is off (#204).

Switching Control off removed the control entities from HA, but the bridge
stayed subscribed to ``<prefix>/<short_id>/control/+/set`` and obeyed whatever
arrived, so anyone who could publish to the broker could still command the
battery.
"""

from __future__ import annotations

import pytest

from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.db import init_db, update_publishing_group

SERIAL = "10060006A02F00000001"
SERIAL_2 = "10060006A02F00000002"


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def handle_command(self, slug: str, payload: str, requester: str = "") -> None:
        self.calls.append((slug, payload))


class _Msg:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


def _publisher() -> tuple[MqttPublisher, _Handler, str]:
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    handler = _Handler()
    pub.set_command_handler(handler)
    return pub, handler, pub._device_info.short_id


async def test_commands_obeyed_while_control_is_on(db):
    pub, handler, sid = _publisher()
    await pub.sync_groups(db)
    assert sid in pub._command_targets()
    await pub._handle_mqtt_message(_Msg(f"fwh_mb/{sid}/control/battery_command/set", b"Release"))
    assert handler.calls == [("battery_command", "Release")]


async def test_control_off_refuses_commands_and_drops_the_subscription(db):
    pub, handler, sid = _publisher()
    await pub.sync_groups(db)
    await update_publishing_group(db, "control", enabled=False)
    await pub.sync_groups(db)

    assert pub._command_targets() == {}
    await pub._handle_mqtt_message(
        _Msg(f"fwh_mb/{sid}/control/battery_command/set", b"Force Discharge")
    )
    await pub._handle_mqtt_message(_Msg(f"fwh_mb/{sid}/control/operating_mode/set", b"x"))
    assert handler.calls == []


async def test_control_back_on_restores_commands(db):
    pub, handler, sid = _publisher()
    await update_publishing_group(db, "control", enabled=False)
    await pub.sync_groups(db)
    await update_publishing_group(db, "control", enabled=True)
    await pub.sync_groups(db)

    assert sid in pub._command_targets()
    await pub._handle_mqtt_message(_Msg(f"fwh_mb/{sid}/control/battery_command/set", b"Release"))
    assert handler.calls == [("battery_command", "Release")]


async def test_second_gateway_follows_the_group(db):
    """A registered gateway kept the group set copied at registration."""
    pub, _, _ = _publisher()
    second = _Handler()
    dev = pub.register_device("gw2", DeviceInfo(serial=SERIAL_2), command_handler=second)
    sid2 = dev.device_info.short_id
    assert any(e.is_control for e in dev.entities)

    await update_publishing_group(db, "control", enabled=False)
    await pub.sync_groups(db)

    assert not any(e.is_control for e in dev.entities)
    assert not dev.discovery_published  # re-announced, so HA drops the controls
    assert sid2 not in pub._command_targets()
    await pub._handle_mqtt_message(
        _Msg(f"fwh_mb/{sid2}/control/battery_command/set", b"Force Charge")
    )
    assert second.calls == []


class _FakeClient:
    """Records subscribe/unsubscribe; the run loop's only other calls."""

    def __init__(self) -> None:
        self.subs: set[str] = set()
        self.log: list[tuple[str, str]] = []
        self.messages = self._never()

    async def _never(self):
        import asyncio

        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def subscribe(self, topic):
        self.subs.add(topic)
        self.log.append(("sub", topic))

    async def unsubscribe(self, topic):
        self.subs.discard(topic)
        self.log.append(("unsub", topic))

    async def publish(self, topic, payload, retain=False, qos=0):
        pass


async def test_run_loop_unsubscribes_when_control_goes_off(db, monkeypatch):
    import asyncio

    from franklinwh_bridge.publish import mqtt_publisher as mod

    pub, _, sid = _publisher()
    await pub.sync_groups(db)
    client = _FakeClient()
    monkeypatch.setattr(mod.aiomqtt, "Client", lambda **kw: client)
    pub._discovery_paused = True  # discovery is not under test here
    topic = f"fwh_mb/{sid}/control/+/set"

    task = asyncio.create_task(pub._run_loop())
    try:
        for _ in range(50):
            if topic in client.subs:
                break
            await asyncio.sleep(0.05)
        assert topic in client.subs

        await update_publishing_group(db, "control", enabled=False)
        await pub.sync_groups(db)
        for _ in range(60):
            if topic not in client.subs:
                break
            await asyncio.sleep(0.05)
        assert topic not in client.subs
        assert client.log == [("sub", topic), ("unsub", topic)]
    finally:
        pub._stop_event.set()
        await asyncio.wait_for(task, timeout=5)
