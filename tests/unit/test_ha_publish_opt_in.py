"""Home Assistant publishing is opt-in and switchable live (#96).

- The primary (default) gateway honours "Publish to Home Assistant": off
  tombstones its discovery + availability and drops its samples quietly; on
  binds it again and re-publishes discovery. No restart.
- The global MQTT "Enabled" starts/stops the publisher when saved.
- The setup wizard's demo gateway starts unpublished, and the wizard's
  "Publish <gateway>?" answer goes through an admin-only setup endpoint.

No network: MQTT clients are fakes, and the app's own broker is pointed at a
port nothing listens on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.publish import mqtt_publisher as mp
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.db import get_gateway

SERIAL = "10060006A02F00000001"


def _drain(pub: MqttPublisher) -> list[tuple[str, str, bool]]:
    out = []
    while not pub._queue.empty():
        m = pub._queue.get_nowait()
        out.append((m.topic, m.payload, m.retain))
    return out


def _sample(gw: str = "default"):
    return SimpleNamespace(gateway_id=gw, points={"soc": 82.4, "battery_power_w": -1200}, ts=0.0)


class _RecordingClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, object, bool]] = []

    async def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic):
        pass


# ── publisher: the default gateway's switch ──────────────────────────────────


async def test_off_tombstones_every_discovery_topic_and_availability():
    pub = MqttPublisher(host="x", topic_prefix="fwtest", discovery_prefix="hatest")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    sid = pub.device_info.short_id

    count = pub.unbind_default_device(tombstone=True)
    msgs = _drain(pub)

    expected = {e.discovery_topic(sid, "fwtest", "hatest") for e in BRIDGE_ENTITIES}
    tombstoned = {t for t, p, r in msgs if t.startswith("hatest/")}
    assert tombstoned == expected and count == len(expected)
    # Empty, retained: that's what makes HA delete the entity.
    assert all(p == "" and r for _, p, r in msgs)
    assert ("fwtest/00000001/availability", "", True) in msgs
    # Configured prefixes, never the defaults.
    assert not any(t.startswith(("homeassistant/", "franklinwh/")) for t, _, _ in msgs)
    assert pub.device_info is None and pub.default_suppressed


async def test_off_includes_promoted_points():
    pub = MqttPublisher(host="x")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    promoted = SimpleNamespace(
        slug="promoted_x",
        discovery_topic=lambda sid, tp, dp: f"{dp}/sensor/{tp}_{sid}/promoted_x/config",
    )
    pub._promoted_entities = [promoted]
    pub.unbind_default_device()
    topics = [t for t, _, _ in _drain(pub)]
    assert "homeassistant/sensor/franklinwh_00000001/promoted_x/config" in topics


async def test_off_drops_samples_without_the_unregistered_warning(monkeypatch, caplog):
    monkeypatch.setattr(mp, "_UNREGISTERED_GRACE_S", -1.0)
    pub = MqttPublisher(host="x")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    pub.unbind_default_device()
    _drain(pub)

    caplog.set_level(logging.WARNING)
    for _ in range(3):
        await pub.queue_sample(_sample())
    assert _drain(pub) == []
    assert "no registered MQTT device" not in caplog.text
    # Command state isn't queued either.
    await pub.publish_command_state()
    assert _drain(pub) == []
    # Its control topics are no longer obeyed.
    assert pub._command_targets() == {}

    # A different gateway with no device still warns — that's still a fault.
    await pub.queue_sample(_sample("stray"))
    assert "no registered MQTT device" in caplog.text


async def test_repeat_off_queues_nothing_more():
    pub = MqttPublisher(host="x")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    pub.unbind_default_device()
    _drain(pub)
    assert pub.unbind_default_device() == 0
    assert _drain(pub) == []


async def test_on_again_republishes_discovery_and_output_is_unchanged():
    """Off then on leaves the default path exactly as it was before."""
    baseline = MqttPublisher(host="x")
    baseline.set_device_info(DeviceInfo(serial=SERIAL))
    await baseline.queue_sample(_sample())
    before = _drain(baseline)
    base_client = _RecordingClient()
    await baseline._publish_discovery(base_client)

    pub = MqttPublisher(host="x")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    pub.unbind_default_device()
    pub.resume_default_device()
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    _drain(pub)
    assert not pub.default_suppressed
    assert pub.state.discovery_published is False  # re-announce on the next tick

    await pub.queue_sample(_sample())
    assert _drain(pub) == before and before

    client = _RecordingClient()
    await pub._publish_discovery(client)
    assert client.published == base_client.published
    soc = next(p for t, p, _ in client.published if t.endswith("_battery_soc/config"))
    assert json.loads(soc)["unique_id"] == "franklinwh_00000001_battery_soc"


# ── publisher: stop(offline=True) ─────────────────────────────────────────────


class _FakeAiomqttClient(_RecordingClient):
    instances: list[_FakeAiomqttClient] = []

    def __init__(self, **kw) -> None:
        super().__init__()
        _FakeAiomqttClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_stop_offline_says_offline_before_disconnecting(monkeypatch):
    _FakeAiomqttClient.instances = []
    monkeypatch.setattr(mp.aiomqtt, "Client", _FakeAiomqttClient)
    pub = MqttPublisher(host="x")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    await pub.start()
    for _ in range(100):
        if pub.state.connected and pub.state.discovery_published:
            break
        await asyncio.sleep(0.01)
    assert pub.state.connected

    await pub.stop(offline=True)
    client = _FakeAiomqttClient.instances[-1]
    avail = [(p, r) for t, p, r in client.published if t == "franklinwh/00000001/availability"]
    assert avail[0] == ("online", True)
    assert avail[-1] == ("offline", True)
    # Discovery is left alone: stopping isn't unpublishing.
    assert not any(t.endswith("/config") and p in ("", b"") for t, p, _ in client.published)
    assert not pub.state.connected


# ── API ───────────────────────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("MODBUS_HOST", raising=False)
    # Nothing listens here: the app's own publisher never reaches a broker.
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


def _fake_default(monkeypatch):
    """A running default gateway with device info, without a real aGate."""
    registry = app.state.registry
    real_get = registry.get
    inst = SimpleNamespace(
        device_info=DeviceInfo(serial=SERIAL),
        status=SimpleNamespace(ac_type=0),
        command_handler=None,
        config=SimpleNamespace(mock=False),
        reader_fn=None,
    )
    monkeypatch.setattr(
        registry, "get", lambda gw_id: inst if gw_id == "default" else real_get(gw_id)
    )
    return inst


async def test_primary_gateway_publish_switch_applies_live(client, monkeypatch):
    pub = app.state.mqtt_publisher
    _fake_default(monkeypatch)
    assert await app.state.wire_default_mqtt()
    assert pub.device_info is not None and pub.device_info.serial == SERIAL
    _drain(pub)

    resp = await client.patch("/api/gateways/default", json={"publish_to_ha": False})
    assert resp.status_code == 200 and resp.json()["publish_to_ha"] == 0
    msgs = _drain(pub)
    sid = "00000001"
    expected = {
        e.discovery_topic(sid, pub.topic_prefix, pub.discovery_prefix) for e in BRIDGE_ENTITIES
    }
    assert expected <= {t for t, p, _ in msgs if p == ""}
    assert (f"{pub.topic_prefix}/{sid}/availability", "", True) in msgs
    assert pub.device_info is None

    # Samples now go nowhere; and a re-wire (gateway restart) keeps it off.
    await pub.queue_sample(_sample())
    assert await app.state.wire_default_mqtt()
    assert _drain(pub) == [] and pub.device_info is None

    resp = await client.patch("/api/gateways/default", json={"publish_to_ha": True})
    assert resp.status_code == 200 and resp.json()["publish_to_ha"] == 1
    assert pub.device_info is not None and pub.device_info.serial == SERIAL
    assert pub.state.discovery_published is False
    await pub.queue_sample(_sample())
    assert any(t.endswith("/battery/battery_soc") for t, _, _ in _drain(pub))


async def test_mqtt_enabled_starts_and_stops_live(client, monkeypatch):
    pub = app.state.mqtt_publisher
    calls: list[tuple] = []

    async def fake_start():
        calls.append(("start",))

    async def fake_stop(offline=False):
        calls.append(("stop", offline))

    monkeypatch.setattr(pub, "start", fake_start)
    monkeypatch.setattr(pub, "stop", fake_stop)

    pub._state.connected = True
    resp = await client.patch("/api/mqtt/config", json={"enabled": False})
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    assert calls == [("stop", True)]
    pub._state.connected = False

    # Reconnect must not override "Enabled" off.
    calls.clear()
    resp = await client.post("/api/mqtt/reconnect")
    assert resp.json()["status"] == "disabled"
    assert calls == [("stop", False)]

    calls.clear()
    resp = await client.patch("/api/mqtt/config", json={"enabled": True})
    assert resp.json()["enabled"] is True
    assert calls == [("start",)]

    # Other fields don't touch the connection.
    calls.clear()
    await client.patch("/api/mqtt/config", json={"qos": 1})
    assert calls == []

    # An add-on still waiting for Mosquitto: the watcher starts it, not us.
    calls.clear()
    monkeypatch.setattr(app.state, "mqtt_broker", {"source": "none", "waiting_for_broker": True})
    await client.patch("/api/mqtt/config", json={"enabled": True})
    assert calls == []


async def test_demo_is_unpublished_and_the_wizard_can_publish_it(client):
    db = app.state.db
    resp = await client.post("/api/setup/demo", json={})
    assert resp.status_code == 200, resp.text
    demo_id = resp.json()["gateway_id"]
    assert resp.json()["gateway"]["publish_to_ha"] == 0
    assert app.state.mqtt_publisher.get_device(demo_id) is None

    checklist = (await client.get("/api/setup/checklist")).json()
    by_id = {g["gateway_id"]: g for g in checklist["gateways"]}
    assert by_id[demo_id]["publish_to_ha"] is False and by_id[demo_id]["mock"] is True
    assert by_id["default"]["publish_to_ha"] is True  # a real aGate stays on

    body = (
        await client.post("/api/setup/publish-to-ha", json={"gateway_id": demo_id, "publish": True})
    ).json()
    assert {g["gateway_id"]: g for g in body["gateways"]}[demo_id]["publish_to_ha"] is True
    assert (await get_gateway(db, demo_id))["publish_to_ha"] == 1

    resp = await client.post(
        "/api/setup/publish-to-ha", json={"gateway_id": "nope", "publish": True}
    )
    assert resp.status_code == 404
    resp = await client.post(
        "/api/setup/publish-to-ha", json={"gateway_id": demo_id, "publish": "maybe"}
    )
    assert resp.status_code == 422


async def test_checklist_entities_skipped_when_nothing_is_published(client):
    await client.patch("/api/gateways/default", json={"publish_to_ha": False})
    items = {i["id"]: i for i in (await client.get("/api/setup/checklist")).json()["items"]}
    assert items["ha_entities"]["status"] == "skipped"
    assert "No gateway is published" in items["ha_entities"]["detail"]


async def test_publish_to_ha_setup_write_is_admin_only(client):
    from franklinwh_bridge.api.auth import get_current_user, require_auth

    viewer = {"id": "v", "username": "v", "role": "viewer", "enabled": True}
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[require_auth] = lambda: viewer
    app.dependency_overrides[get_current_user] = lambda: viewer
    try:
        resp = await client.post(
            "/api/setup/publish-to-ha", json={"gateway_id": "default", "publish": False}
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
    assert (await get_gateway(app.state.db, "default"))["publish_to_ha"] == 1
