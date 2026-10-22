"""#324 v1.2: the bridge's own HA device, with "Bridge problem" and "Bridge heartbeat".

Shapes agreed with the DC bridge (its #286). The problem sensor has no
availability topic, so it reads while a gateway or the bridge is down. Both
configs carry our sw_version marker, so Unpublish and a namespace change remove
them (install-lifecycle E6, E10).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from franklinwh_bridge import workers
from franklinwh_bridge.alert_monitor import AlertMonitor
from franklinwh_bridge.publish import bridge_device
from franklinwh_bridge.publish import mqtt_publisher as mp
from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher
from franklinwh_bridge.publish.namespace import is_ours
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store.db import init_db

T0 = 1_792_536_000.0
PROBLEM_CFG = "homeassistant/binary_sensor/fwh_mb_bridge/bridge_problem/config"
HEARTBEAT_CFG = "homeassistant/sensor/fwh_mb_bridge/bridge_heartbeat/config"


def test_discovery_configs():
    configs = bridge_device.discovery_configs("fwh_mb", "homeassistant", "0.2.37")
    assert list(configs) == [PROBLEM_CFG, HEARTBEAT_CFG]
    problem, heartbeat = configs.values()
    assert problem["unique_id"] == "fwh_mb_bridge_problem"
    assert problem["state_topic"] == problem["json_attributes_topic"] == "fwh_mb/bridge/problem"
    assert (problem["device_class"], problem["entity_category"]) == ("problem", "diagnostic")
    assert heartbeat["unique_id"] == "fwh_mb_bridge_heartbeat"
    assert (heartbeat["device_class"], heartbeat["state_topic"]) == (
        "timestamp", "fwh_mb/bridge/heartbeat")
    for c in configs.values():
        assert "availability_topic" not in c and "availability" not in c
        assert c["device"]["identifiers"] == ["fwh_mb_modbus_bridge"]
        assert is_ours(json.dumps(c))  # so the clean-up recognises it


def test_problem_payload():
    summary = {"level": "critical", "open": 1}
    a = {"id": 7, "gateway_id": "", "code": "GATEWAY_UNREACHABLE", "kind": "condition",
         "severity": "critical", "title": "aGate unreachable", "message": "m",
         "opened_ts": T0, "count": 1, "resolved_ts": None, "acked_ts": None}
    p = bridge_device.problem_payload(summary, [a])
    assert (p["problem"], p["level"], p["open"]) == (True, "critical", 1)
    assert p["alerts"][0]["code"] == "GATEWAY_UNREACHABLE"
    assert p["alerts"][0]["resolved"] is False
    assert bridge_device.problem_payload({"level": "ok", "open": 0}, [])["problem"] is False


def _queued(pub: MqttPublisher) -> list[tuple[str, str, bool]]:
    out = []
    while not pub._queue.empty():
        m = pub._queue.get_nowait()
        out.append((m.topic, m.payload, m.retain))
    return out


def test_states_are_sent_on_change_and_only_while_connected():
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    pub.publish_bridge_problem({"problem": False})
    pub.publish_bridge_heartbeat(T0)
    assert _queued(pub) == []  # not connected: nothing piles up

    pub._state.connected = True
    pub.publish_bridge_problem({"problem": True})
    pub.publish_bridge_problem({"problem": True})  # unchanged: not re-sent
    pub.publish_bridge_heartbeat(T0)
    assert _queued(pub) == [
        ("fwh_mb/bridge/problem", '{"problem": true}', True),
        ("fwh_mb/bridge/heartbeat", "2026-10-20T22:40:00+00:00", True),
    ]
    pub.set_discovery_paused(True)  # Unpublished: nothing goes to HA
    pub.publish_bridge_problem({"problem": False})
    assert _queued(pub) == []


class _Client:
    published: list[tuple[str, object]] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, topic, payload=None, retain=False, qos=0):
        _Client.published.append((topic, payload))


async def test_connect_announces_and_resends_last_problem():
    _Client.published = []
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    pub.publish_bridge_problem({"problem": True})  # while down
    await pub._publish_bridge_discovery(_Client())
    topics = [t for t, _ in _Client.published]
    assert topics == [PROBLEM_CFG, HEARTBEAT_CFG, "fwh_mb/bridge/problem"]


async def test_unpublish_removes_the_bridge_device_even_with_no_gateway(monkeypatch):
    _Client.published = []
    monkeypatch.setattr(mp.aiomqtt, "Client", _Client)
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    count = await pub.unpublish_discovery()
    assert count == 2
    assert dict(_Client.published) == {
        PROBLEM_CFG: b"", HEARTBEAT_CFG: b"",
        "fwh_mb/bridge/problem": b"", "fwh_mb/bridge/heartbeat": b""}


class _Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload, self.retain = topic, payload, True


class _SubClient(_Client):
    retained: dict[str, bytes] = {}

    def __init__(self, *a, **k) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, topics, qos=0, **kw):
        for t, _ in topics:
            if t in _SubClient.retained:
                self._inbox.put_nowait(_Msg(t, _SubClient.retained[t]))

    @property
    def messages(self):
        async def gen():
            while True:
                yield await self._inbox.get()
        return gen()


async def test_namespace_change_removes_the_old_bridge_device(monkeypatch):
    _Client.published = []
    old = bridge_device.discovery_configs("fwh_old", "homeassistant", "0.2.37")
    _SubClient.retained = {t: json.dumps(c).encode() for t, c in old.items()}
    monkeypatch.setattr(mp.aiomqtt, "Client", _SubClient)
    pub = MqttPublisher(host="x", topic_prefix="fwh_old")
    counts = await pub.tombstone_own_discovery("fwh_old", set(), window_s=0.2)
    assert counts["removed_ours"] == 2
    assert {t for t, p in _Client.published if p == b""} == set(old)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "bridge.db")
    yield conn
    await conn.close()


class _Pub:
    def __init__(self):
        self.problems, self.beats = [], []

    def publish_bridge_problem(self, payload):
        self.problems.append(payload)

    def publish_bridge_heartbeat(self, ts):
        self.beats.append(ts)


async def test_monitor_publishes_problem_and_heartbeat(db):
    pub = _Pub()
    mon = AlertMonitor(db, SimpleNamespace(instances={}),
                       worker_registry=workers.WorkerRegistry(), publisher=lambda: pub)
    await mon.check(now=T0)
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "never answered",
                                     gateway_id="gw9", now=T0)
    await mon.check(now=T0 + 15)
    await mon.check(now=T0 + 60)
    assert pub.beats == [T0, T0 + 60]
    assert pub.problems[0]["problem"] is False
    last = pub.problems[-1]
    assert (last["problem"], last["level"], last["open"]) == (True, "critical", 1)
    assert last["alerts"][0]["gateway_id"] == "gw9"
