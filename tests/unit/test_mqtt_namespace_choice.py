"""Choosing and safely changing the MQTT namespace (#87 part B), and a client
id of its own per install (#118).

No network: the broker is a fake that holds retained payloads, and the app's own
publisher is pointed at a port nothing listens on.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.publish import mqtt_publisher as mp
from franklinwh_bridge.publish import namespace as ns
from franklinwh_bridge.publish.entities import get_entity_by_slug
from franklinwh_bridge.publish.mqtt_publisher import (
    DeviceInfo,
    MqttPublisher,
    build_discovery_payload,
)
from franklinwh_bridge.store.db import (
    get_app_config,
    get_mqtt_config,
    init_db,
    set_app_config,
    set_mqtt_config,
)

SERIAL = "TESTSERIAL0001"
SID = SERIAL[-8:]  # "RIAL0001"


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ok", ["fwh_mb", "franklinwh", "fwh_home", "fwh_ab", "fwh_a1b2c3d4"])
def test_allowed_namespaces(ok):
    assert ns.validate_namespace(ok) is None


@pytest.mark.parametrize("reserved", ["fwhai", "fwh_ai", "fwh_lb"])
def test_other_producers_namespaces_are_reserved(reserved):
    assert "reserved" in ns.validate_namespace(reserved)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "fwh_a",
        "fwh_abcdefghi",
        "fwh_AB",
        "fwh-mb",
        "mybridge",
        "franklinwh2",
        "fwh_m/b",
        "homeassistant",
    ],
)
def test_other_values_are_refused(bad):
    assert ns.validate_namespace(bad)


def test_policy_is_one_constant():
    p = ns.NAMESPACE_POLICY
    assert list(p.presets) == ["fwh_mb", "franklinwh"]
    assert set(p.reserved) == {"fwhai", "fwh_ai", "fwh_lb"}
    # Every reserved value that matches the custom pattern is still refused.
    assert any(re.fullmatch(p.custom_pattern, r) for r in p.reserved)


def test_is_ours_reads_the_device_sw_version():
    ours = {"device": {"sw_version": "Modbus Bridge v0.1.0"}}
    fwhai = {"device": {"sw_version": "FHAI: v0.6.76"}}
    abbreviated = {"dev": {"sw": "Modbus Bridge v0.1.0"}}
    assert ns.is_ours(json.dumps(ours)) and ns.is_ours(json.dumps(abbreviated).encode())
    assert not ns.is_ours(json.dumps(fwhai))
    for junk in (None, "", b"", "not json", "[]", json.dumps({"device": "x"})):
        assert not ns.is_ours(junk)


# ── first start: which namespace ─────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "bridge.db")
    yield conn
    await conn.close()


async def _add_gateway(db, gw_id="default", host="", serial=None, mock=0):
    await db.execute(
        "INSERT INTO gateways (id, name, host, port, unit_id, enabled, created_at, serial, mock)"
        " VALUES (?, 'aGate', ?, 502, 1, 1, 0, ?, ?)",
        (gw_id, host, serial, mock),
    )
    await db.commit()


async def test_fresh_install_preselects_fwh_mb_but_leaves_the_choice_pending(db):
    """A fresh install chooses nothing by itself: the user does (the wizard)."""
    choice = await ns.choose_initial_namespace(db, env_value="")
    assert choice == {"prefix": "fwh_mb", "reason": "pending", "pending": True}
    assert (await get_mqtt_config(db))["topic_prefix"] == "fwh_mb"
    assert not await get_app_config(db, ns.CHOSEN_KEY)
    assert await ns.is_pending(db)
    # Still pending at the next start — even once a gateway is configured.
    await _add_gateway(db, host="192.0.2.10")
    assert (await ns.choose_initial_namespace(db, env_value=""))["pending"] is True


async def test_unconfigured_default_gateway_row_is_still_fresh(db):
    await _add_gateway(db, host="")
    assert (await ns.choose_initial_namespace(db))["prefix"] == "fwh_mb"


@pytest.mark.parametrize(
    "row",
    [
        {"host": "192.0.2.10"},
        {"host": "", "serial": SERIAL},
        {"gw_id": "demo", "host": "", "mock": 1},
    ],
)
async def test_existing_install_keeps_franklinwh(db, row):
    """The live bridge has a configured gateway: it must stay on franklinwh."""
    await _add_gateway(db, **row)
    choice = await ns.choose_initial_namespace(db)
    assert choice == {**choice, "prefix": "franklinwh", "reason": "existing_install"}
    assert (await get_mqtt_config(db))["topic_prefix"] == "franklinwh"


async def test_something_published_before_keeps_franklinwh(db):
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    assert (await ns.choose_initial_namespace(db))["prefix"] == "franklinwh"


async def test_a_chosen_custom_prefix_is_kept(db):
    await set_mqtt_config(db, {"topic_prefix": "fwh_home"})
    assert (await ns.choose_initial_namespace(db))["prefix"] == "fwh_home"


async def test_decided_once(db):
    await _add_gateway(db, host="192.0.2.10")
    await ns.choose_initial_namespace(db)
    # Even with the gateway gone, a later start doesn't move it.
    await db.execute("DELETE FROM gateways")
    await db.commit()
    assert (await ns.choose_initial_namespace(db))["prefix"] == "franklinwh"
    assert (await get_mqtt_config(db))["topic_prefix"] == "franklinwh"


# ── #118: client id ──────────────────────────────────────────────────────────


async def test_shared_client_id_is_replaced_once(db):
    assert (await get_mqtt_config(db))["client_id"] == "franklinwh_bridge"
    cid = await ns.ensure_client_id(db)
    assert re.fullmatch(r"franklinwh_bridge_[0-9a-f]{8}", cid)
    assert (await get_mqtt_config(db))["client_id"] == cid
    assert await ns.ensure_client_id(db) == cid


async def test_explicit_client_id_is_kept(db):
    await set_mqtt_config(db, {"client_id": "my_bridge"})
    assert await ns.ensure_client_id(db) == "my_bridge"


def test_repeated_drops_warn_about_the_client_id(caplog):
    pub = MqttPublisher(host="x", client_id="franklinwh_bridge_0a1b2c3d")
    with caplog.at_level("WARNING"):
        for _ in range(4):
            pub._note_dropped()
    hits = [r for r in caplog.records if "another client may be" in r.getMessage()]
    assert len(hits) == 1 and "franklinwh_bridge_0a1b2c3d" in hits[0].getMessage()


# ── the change flow ──────────────────────────────────────────────────────────


class _Msg:
    def __init__(self, topic, payload, retain=True):
        self.topic = topic
        self.payload = payload
        self.retain = retain


class FakeBroker:
    """Retained store shared by every fake client; records every publish."""

    def __init__(self) -> None:
        self.retained: dict[str, bytes] = {}
        self.published: list[tuple[str, object, bool]] = []
        self.clients: list[_FakeClient] = []
        self.refuse = False


BROKER = FakeBroker()


class _FakeClient:
    def __init__(self, **kw) -> None:
        self.kw = kw
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.subscribed: list[str] = []
        BROKER.clients.append(self)

    async def __aenter__(self):
        if BROKER.refuse:
            raise mp.aiomqtt.MqttError("connection refused")
        return self

    async def __aexit__(self, *exc):
        return False

    async def subscribe(self, topic, qos=0, **kw):
        topics = [t for t, _ in topic] if isinstance(topic, list) else [topic]
        for t in topics:
            self.subscribed.append(t)
            if BROKER.retained.get(t):
                self._inbox.put_nowait(_Msg(t, BROKER.retained[t]))

    async def publish(self, topic, payload=None, retain=False, qos=0):
        BROKER.published.append((topic, payload, retain))
        if retain:
            BROKER.retained[topic] = payload

    @property
    def messages(self):
        async def gen():
            while True:
                yield await self._inbox.get()

        return gen()


@pytest.fixture
def broker(monkeypatch):
    global BROKER
    BROKER = FakeBroker()
    monkeypatch.setattr(mp.aiomqtt, "Client", _FakeClient)
    return BROKER


def _ours(slug: str, prefix="franklinwh") -> bytes:
    ent = get_entity_by_slug(slug)
    return json.dumps(
        build_discovery_payload(ent, DeviceInfo(serial=SERIAL), app_version="0.1.0", prefix=prefix)
    ).encode()


def _fwhai(slug: str) -> bytes:
    return json.dumps(
        {
            "unique_id": f"franklinwh_{SID}_{slug}",
            "command_topic": f"franklinwh/{SID}/control/{slug}/set",
            "device": {"identifiers": [f"franklinwh_{SERIAL}"], "sw_version": "FHAI: v0.6.76"},
        }
    ).encode()


def _disc(slug: str, prefix="franklinwh") -> str:
    return get_entity_by_slug(slug).discovery_topic(SID, prefix)


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def handle_command(self, slug, payload, requester=""):
        self.calls.append((slug, payload))


def _publisher() -> MqttPublisher:
    pub = MqttPublisher(host="x", client_id="franklinwh_bridge_0a1b2c3d")
    pub.set_device_info(DeviceInfo(serial=SERIAL))
    pub.set_command_handler(_Handler())
    return pub


async def test_change_removes_only_this_bridges_old_entities(db, broker):
    avail = f"franklinwh/{SID}/availability"
    state = f"franklinwh/{SID}/battery/battery_soc"
    broker.retained.update(
        {
            _disc("battery_soc"): _ours("battery_soc"),
            _disc("grid_power_kw"): _ours("grid_power_kw"),
            _disc("operating_mode"): _fwhai("operating_mode"),  # FWHAI overwrote it (#120)
            avail: b"online",
            state: b"82.4",
        }
    )
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    pub = _publisher()

    result = await ns.change_namespace(db, pub, "fwh_mb", confirm=True, actor="admin", window_s=0.2)

    assert result["status"] == "changed" and result["from"] == "franklinwh"
    assert (result["removed_ours"], result["left_foreign"]) == (2, 1)
    assert result["not_found"] > 0
    emptied = {t for t, p, r in broker.published if p == b"" and r}
    assert emptied == {_disc("battery_soc"), _disc("grid_power_kw")}
    # FWHAI's config, the shared availability and the state are untouched.
    assert broker.retained[_disc("operating_mode")] == _fwhai("operating_mode")
    assert broker.retained[avail] == b"online" and broker.retained[state] == b"82.4"
    assert not any(t in (avail, state) for t, _, _ in broker.published)
    # The scan didn't reuse our client id (#118) and only read our own topics.
    scan = broker.clients[0]
    assert scan.kw["identifier"] != "franklinwh_bridge_0a1b2c3d"
    assert all(
        t.startswith("homeassistant/") and f"franklinwh_{SID}_" in t for t in scan.subscribed
    )

    # Saved, recorded and audited.
    assert (await get_mqtt_config(db))["topic_prefix"] == "fwh_mb"
    assert json.loads(await get_app_config(db, ns.CHOSEN_KEY))["reason"] == "changed"
    async with db.execute(
        "SELECT detail FROM startup_log WHERE event = 'mqtt_namespace_change'"
    ) as cur:
        detail = (await cur.fetchone())[0]
    assert "franklinwh → fwh_mb" in detail and "removed 2" in detail


async def test_publisher_is_switched_to_the_new_namespace(db, broker):
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    pub = _publisher()
    await ns.change_namespace(db, pub, "fwh_mb", confirm=True, window_s=0.05)

    assert pub.topic_prefix == "fwh_mb" and not pub.state.discovery_published
    payload = build_discovery_payload(
        get_entity_by_slug("operating_mode"), pub.device_info, prefix=pub.topic_prefix
    )
    assert payload["unique_id"] == f"fwh_mb_{SID}_operating_mode"
    assert payload["device"]["identifiers"] == [f"fwh_mb_modbus_{SERIAL}"]
    assert payload["command_topic"] == f"fwh_mb/{SID}/control/operating_mode/set"
    handler = pub._command_handler
    await pub._handle_mqtt_message(_Msg(f"franklinwh/{SID}/control/operating_mode/set", b"x"))
    await pub._handle_mqtt_message(_Msg(f"fwh_mb/{SID}/control/operating_mode/set", b"y"))
    assert handler.calls == [("operating_mode", "y")]


async def test_running_publisher_reconnects_under_the_new_namespace(db, broker):
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    pub = _publisher()
    await pub.start()
    try:
        await ns.change_namespace(db, pub, "fwh_mb", confirm=True, window_s=0.05)
        for _ in range(200):
            if pub.state.discovery_published and broker.clients[-1].subscribed:
                break
            await asyncio.sleep(0.01)
        live = broker.clients[-1]
        assert live.subscribed == [f"fwh_mb/{SID}/control/+/set"]
        assert broker.retained[f"fwh_mb/{SID}/availability"] == "online"
        assert _disc("battery_soc", "fwh_mb") in broker.retained
    finally:
        await pub.stop()


async def test_queued_old_namespace_state_is_dropped(db, broker):
    pub = _publisher()
    pub._queue.put_nowait(
        mp.MqttMessage(topic=f"franklinwh/{SID}/battery/battery_soc", payload="1", retain=True)
    )
    pub._queue.put_nowait(mp.MqttMessage(topic="other/topic", payload="1"))
    pub.switch_namespace("fwh_mb")
    left = [pub._queue.get_nowait().topic for _ in range(pub._queue.qsize())]
    assert left == ["other/topic"]


async def test_published_install_needs_confirmation(db, broker):
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    pub = _publisher()
    with pytest.raises(ns.NamespaceError) as err:
        await ns.change_namespace(db, pub, "fwh_mb", confirm=False)
    assert err.value.status == 409
    assert pub.topic_prefix == "franklinwh" and not broker.clients


async def test_unpublished_install_changes_without_touching_the_broker(db, broker):
    pub = MqttPublisher(host="x")
    result = await ns.change_namespace(db, pub, "fwh_home", confirm=False)
    assert result["status"] == "changed" and result["scanned"] is False
    assert pub.topic_prefix == "fwh_home" and not broker.clients


async def test_unreachable_broker_changes_nothing(db, broker):
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    broker.refuse = True
    pub = _publisher()
    with pytest.raises(ns.NamespaceError) as err:
        await ns.change_namespace(db, pub, "fwh_mb", confirm=True, window_s=0.05)
    assert err.value.status == 503
    assert pub.topic_prefix == "franklinwh"
    assert (await get_mqtt_config(db))["topic_prefix"] == "franklinwh"


async def test_reserved_and_unchanged(db, broker):
    pub = _publisher()
    with pytest.raises(ns.NamespaceError) as err:
        await ns.change_namespace(db, pub, "fwhai", confirm=True)
    assert err.value.status == 422
    assert (await ns.change_namespace(db, pub, "franklinwh", confirm=True))["status"] == "unchanged"


async def test_offline_gateways_on_file_are_included(db, broker):
    await _add_gateway(db, host="192.0.2.10", serial=SERIAL)
    await _add_gateway(db, gw_id="Demo GW", host="", mock=1)
    pub = MqttPublisher(host="x")  # nothing bound right now
    assert await ns.published_short_ids(db, pub) == {SID, "demo_gw"}


# ── API ───────────────────────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
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


async def test_fresh_app_starts_on_fwh_mb_with_its_own_client_id(client):
    """A fresh install, even one whose aGate comes from MODBUS_HOST (conftest)."""
    cfg = (await client.get("/api/mqtt/config")).json()
    assert cfg["topic_prefix"] == "fwh_mb"
    assert re.fullmatch(r"franklinwh_bridge_[0-9a-f]{8}", cfg["client_id"])
    assert app.state.mqtt_publisher.topic_prefix == "fwh_mb"
    info = (await client.get("/api/mqtt/namespace")).json()
    assert info["topic_prefix"] == "fwh_mb" and info["recommended"] == "fwh_mb"
    assert info["published"] is False and "fwh_lb" in info["reserved"]


async def test_api_changes_before_anything_is_published(client):
    resp = await client.post("/api/mqtt/namespace", json={"topic_prefix": "franklinwh"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scanned"] is False
    assert (await client.get("/api/mqtt/config")).json()["topic_prefix"] == "franklinwh"
    assert app.state.mqtt_publisher.topic_prefix == "franklinwh"


async def test_api_refuses_reserved_and_unconfirmed(client):
    resp = await client.post("/api/mqtt/namespace", json={"topic_prefix": "fwh_lb"})
    assert resp.status_code == 422 and "reserved" in resp.json()["detail"]
    await set_app_config(app.state.db, ns.PUBLISHED_KEY, "1")
    resp = await client.post("/api/mqtt/namespace", json={"topic_prefix": "fwh_home"})
    assert resp.status_code == 409 and "Confirm" in resp.json()["detail"]


async def test_patch_points_to_the_namespace_endpoint(client):
    resp = await client.patch("/api/mqtt/config", json={"topic_prefix": "fwh_home"})
    assert resp.status_code == 409
    assert "/api/mqtt/namespace" in resp.json()["detail"]
    resp = await client.patch("/api/mqtt/config", json={"discovery_prefix": "other"})
    assert resp.status_code == 409 and "can't be changed" in resp.json()["detail"]


async def test_wizard_checklist_offers_the_namespace(client):
    await client.post("/api/setup/use-ha", json={"use_ha": True})
    body = (await client.get("/api/setup/checklist")).json()
    assert body["namespace"]["topic_prefix"] == "fwh_mb"
    assert [p["value"] for p in body["namespace"]["presets"]] == ["fwh_mb", "franklinwh"]
