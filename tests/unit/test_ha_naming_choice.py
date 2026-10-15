"""A fresh install chooses its Home Assistant naming before anything is published.

The owner's requirement (2026-10-15): a FRESH install gets an explicit, informed
choice of namespace — it becomes part of every HA unique_id, so it is treated as
permanent. Until it is chosen the publisher connects but publishes nothing.
Existing installs (the live bridge) are unaffected.

No network: the broker is the fake retained store from the namespace tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.publish import namespace as ns
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.db import (
    get_app_config,
    get_mqtt_config,
    init_db,
    set_app_config,
)
from tests.unit.test_mqtt_namespace_choice import (  # noqa: F401  (fixture)
    SERIAL,
    SID,
    _add_gateway,
    _disc,
    _fwhai,
    _ours,
    broker,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "bridge.db")
    yield conn
    await conn.close()


# ── which installs wait ──────────────────────────────────────────────────────


async def test_env_var_chooses_on_a_fresh_install(db):
    choice = await ns.choose_initial_namespace(db, env_value="franklinwh")
    assert choice["prefix"] == "franklinwh" and choice["reason"] == "env"
    assert not choice.get("pending") and not await ns.is_pending(db)
    assert (await get_mqtt_config(db))["topic_prefix"] == "franklinwh"
    assert json.loads(await get_app_config(db, ns.CHOSEN_KEY))["reason"] == "env"


async def test_env_var_also_resolves_a_pending_choice_at_a_later_start(db):
    assert (await ns.choose_initial_namespace(db, env_value=""))["pending"]
    choice = await ns.choose_initial_namespace(db, env_value="fwh_home")
    assert choice["prefix"] == "fwh_home" and not await ns.is_pending(db)


@pytest.mark.parametrize("bad", ["fwhai", "fwh_lb", "Not Valid"])
async def test_a_refused_env_value_leaves_the_choice_pending(db, bad):
    choice = await ns.choose_initial_namespace(db, env_value=bad)
    assert choice["pending"] is True and choice["prefix"] == "fwh_mb"


async def test_existing_install_is_unaffected_and_ignores_the_env_var(db, caplog):
    """The live bridge: a configured gateway, franklinwh, nothing pending."""
    await _add_gateway(db, host="192.0.2.10", serial=SERIAL)
    with caplog.at_level("WARNING"):
        choice = await ns.choose_initial_namespace(db, env_value="fwh_mb")
    assert choice["prefix"] == "franklinwh" and choice["reason"] == "existing_install"
    assert not await ns.is_pending(db)
    # A later start with the env var set doesn't move it either: it says so.
    with caplog.at_level("WARNING"):
        again = await ns.choose_initial_namespace(db, env_value="fwh_mb")
    assert again["prefix"] == "franklinwh"
    assert any("ignored" in r.getMessage() for r in caplog.records)
    assert (await get_mqtt_config(db))["topic_prefix"] == "franklinwh"


# ── the publisher holds ──────────────────────────────────────────────────────


def _sample() -> Sample:
    return Sample.now("default", {"soc": 55.0})


async def _settle(cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_held_publisher_publishes_nothing_until_released(broker):  # noqa: F811
    pub = MqttPublisher(host="x", client_id="franklinwh_bridge_0a1b2c3d", topic_prefix="fwh_mb")
    pub.set_device_info(DeviceInfo(serial=SERIAL))

    class _H:
        virtual_points: dict = {}

        async def handle_command(self, *a, **k):
            pass

    pub.set_command_handler(_H())
    pub.hold()
    await pub.start()
    try:
        assert await _settle(lambda: pub.state.connected)
        await pub.queue_sample(_sample())
        await asyncio.sleep(0.3)
        assert broker.published == []  # no availability, discovery or state
        assert all(c.subscribed == [] for c in broker.clients)  # no commands
        assert pub.state.discovery_published is False

        pub.release()
        assert await _settle(lambda: pub.state.discovery_published)
        assert await _settle(lambda: bool(broker.clients[-1].subscribed))
        assert broker.retained[f"fwh_mb/{SID}/availability"] == "online"
        assert _disc("battery_soc", "fwh_mb") in broker.retained
        assert broker.clients[-1].subscribed == [f"fwh_mb/{SID}/control/+/set"]
    finally:
        await pub.stop()


async def test_hold_covers_devices_registered_later(broker):  # noqa: F811
    """The 15 s reconcile (#128) registers extra gateways after start: held too."""
    pub = MqttPublisher(host="x", client_id="franklinwh_bridge_0a1b2c3d", topic_prefix="fwh_mb")
    pub.hold()
    await pub.start()
    try:
        assert await _settle(lambda: pub.state.connected)
        info = DeviceInfo(serial="TESTSERIAL0000EF01", gateway_id="agate_ef01")
        pub.register_device("agate_ef01", info)
        await pub.queue_sample(Sample.now("agate_ef01", {"soc": 40.0}))
        await pub.unregister_device("agate_ef01", tombstone=True)  # a re-bind
        pub.register_device("agate_ef01", info)
        await asyncio.sleep(0.3)
        assert broker.published == []

        pub.release()
        topic = _disc("battery_soc", "fwh_mb").replace(SID, "agate_ef01")
        assert await _settle(lambda: topic in broker.retained)
        assert broker.retained["fwh_mb/agate_ef01/availability"] == "online"
        # The re-bind's tombstones, queued while held, were never sent: the
        # entity was never emptied and the device never said "offline".
        sent = [(t, p) for t, p, _ in broker.published]
        assert (topic, "") not in sent and (topic, b"") not in sent
        assert ("fwh_mb/agate_ef01/availability", "offline") not in sent
    finally:
        await pub.stop()


# ── choosing ─────────────────────────────────────────────────────────────────


async def test_choice_needs_acknowledgement_and_an_allowed_value(db):
    await ns.choose_initial_namespace(db, env_value="")
    pub = MqttPublisher(host="x")
    pub.hold()
    with pytest.raises(ns.NamespaceError) as err:
        await ns.choose_namespace(db, pub, "fwh_mb", acknowledged=False)
    assert err.value.status == 422 and "permanent" in str(err.value)
    for bad in ("fwhai", "fwh_lb", "fwh-x", ""):
        with pytest.raises(ns.NamespaceError) as err:
            await ns.choose_namespace(db, pub, bad, acknowledged=True)
        assert err.value.status == 422
    assert pub.held and await ns.is_pending(db)


async def test_choice_records_switches_and_releases(db):
    await ns.choose_initial_namespace(db, env_value="")
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    pub.hold()
    result = await ns.choose_namespace(db, pub, "franklinwh", acknowledged=True, actor="admin")
    assert result == {"status": "chosen", "topic_prefix": "franklinwh"}
    assert not pub.held and pub.topic_prefix == "franklinwh"
    assert not await ns.is_pending(db)
    chosen = json.loads(await get_app_config(db, ns.CHOSEN_KEY))
    assert chosen["prefix"] == "franklinwh" and chosen["by"] == "admin"


async def test_published_install_is_refused_with_409(db):
    await _add_gateway(db, host="192.0.2.10", serial=SERIAL)
    await ns.choose_initial_namespace(db, env_value="")
    await set_app_config(db, ns.PUBLISHED_KEY, "1")
    with pytest.raises(ns.NamespaceError) as err:
        await ns.choose_namespace(db, MqttPublisher(host="x"), "fwh_mb", acknowledged=True)
    assert err.value.status == 409 and "Change namespace" in str(err.value)


async def test_settings_change_while_pending_does_not_count_as_the_choice(db):
    await ns.choose_initial_namespace(db, env_value="")
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    pub.hold()
    await ns.change_namespace(db, pub, "fwh_home", confirm=False)
    assert pub.topic_prefix == "fwh_home" and pub.held and await ns.is_pending(db)


# ── preview ──────────────────────────────────────────────────────────────────


def test_preview_names_the_device_and_ids():
    rows = [
        {"id": "default", "name": "aGate", "serial": "TESTSERIAL0000ABCD"},
        {"id": "demo", "name": "Demo aGate", "serial": "MOCK-demo", "mock": 1, "publish_to_ha": 0},
    ]
    out = ns.preview_namespace("fwh_mb", rows)
    real, demo = out
    assert real["device_name"] == "FranklinWH Modbus 0000ABCD"
    assert real["device_identifier"] == "fwh_mb_modbus_TESTSERIAL0000ABCD"
    assert real["topic_root"] == "fwh_mb/0000ABCD/"
    ids = {e["slug"]: e for e in real["entities"]}
    assert set(ids) == {"battery_soc", "grid_power_kw", "operating_mode"}
    assert ids["battery_soc"]["unique_id"] == "fwh_mb_0000ABCD_battery_soc"
    assert ids["battery_soc"]["discovery_topic"] == (
        "homeassistant/sensor/fwh_mb_0000ABCD_battery_soc/config"
    )
    assert ids["battery_soc"]["entity_id"].startswith("sensor.franklinwh_modbus_0000abcd_")
    assert ids["operating_mode"]["command_topic"] == "fwh_mb/0000ABCD/control/operating_mode/set"
    assert demo["device_name"] == "FranklinWH Modbus demo (mock)" and demo["mock"]
    assert demo["publish_to_ha"] is False
    assert {e["slug"] for e in demo["entities"]} == {"battery_soc", "grid_power_kw"}


def test_preview_of_an_added_gateway_names_by_serial_ids_by_gateway_id():
    """0.2.5 (#128): name = short serial for every gateway; ids = gateway id."""
    rows = [{"id": "agate_ef01", "name": "aGate EF01", "serial": "TESTSERIAL0000EF01"}]
    (row,) = ns.preview_namespace("fwh_mb", rows)
    assert row["device_name"] == "FranklinWH Modbus 0000EF01"
    assert row["topic_root"] == "fwh_mb/agate_ef01/"
    ids = {e["slug"]: e for e in row["entities"]}
    assert ids["battery_soc"]["unique_id"] == "fwh_mb_agate_ef01_battery_soc"
    assert ids["battery_soc"]["entity_id"].startswith("sensor.franklinwh_modbus_0000ef01_")
    # Not connected yet: the name waits for the serial; the ids don't need it.
    (row,) = ns.preview_namespace("fwh_mb", [{"id": "agate_ef01", "serial": None}])
    assert row["device_name"] == "FranklinWH Modbus <last 8 of serial>"
    assert row["topic_root"] == "fwh_mb/agate_ef01/"


def test_preview_without_a_serial_yet_uses_placeholders():
    (row,) = ns.preview_namespace("fwh_mb", [{"id": "default", "serial": None}])
    assert row["serial_known"] is False
    assert row["device_name"] == "FranklinWH Modbus <last 8 of serial>"
    assert row["device_identifier"] == "fwh_mb_modbus_<serial>"
    assert row["entities"][0]["unique_id"] == "fwh_mb_<last 8 of serial>_battery_soc"


# ── a previous install on the broker ─────────────────────────────────────────


async def test_previous_install_found_under_its_namespace(db, broker):  # noqa: F811
    await _add_gateway(db, host="192.0.2.10", serial=SERIAL)
    broker.retained[_disc("battery_soc", "franklinwh")] = _ours("battery_soc")
    broker.retained[_disc("grid_power_kw", "franklinwh")] = _ours("grid_power_kw")
    pub = MqttPublisher(host="x", client_id="franklinwh_bridge_0a1b2c3d", topic_prefix="fwh_mb")
    found = await ns.detect_previous_install(db, pub, window_s=0.1)
    assert found["checked"] and found["found"] and found["prefix"] == "franklinwh"
    assert found["ours"] == {"franklinwh": 2}
    # Read-only: nothing written, and not on our own client id (#118).
    assert broker.published == []
    assert broker.clients[0].kw["identifier"] != "franklinwh_bridge_0a1b2c3d"


async def test_another_producers_entities_are_not_a_previous_install(db, broker):  # noqa: F811
    await _add_gateway(db, host="192.0.2.10", serial=SERIAL)
    broker.retained[_disc("operating_mode", "franklinwh")] = _fwhai("operating_mode")
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    found = await ns.detect_previous_install(db, pub, window_s=0.1)
    assert found["checked"] and not found["found"] and found["prefix"] is None
    assert found["foreign"] == {"franklinwh": 1}


async def test_unreachable_broker_is_reported_not_raised(db, broker):  # noqa: F811
    await _add_gateway(db, host="192.0.2.10", serial=SERIAL)
    broker.refuse = True
    found = await ns.detect_previous_install(db, MqttPublisher(host="x"), window_s=0.1)
    assert not found["checked"] and "broker" in found["error"]


# ── the app ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1")
    monkeypatch.delenv(ns.NAMESPACE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_fresh_app_holds_publishing_and_says_so(client):
    assert app.state.mqtt_publisher.held
    state = (await client.get("/api/setup/state")).json()
    assert state["ha_naming_pending"] is True
    body = (await client.get("/api/setup/ha-naming")).json()
    assert body["pending"] and body["can_choose"] and body["current"] == "fwh_mb"
    assert [c["value"] for c in body["choices"]] == ["fwh_mb", "franklinwh"]
    assert {r["value"] for r in body["reserved"]} == {"fwhai", "fwh_ai", "fwh_lb"}
    assert body["env_var"] == "MQTT_NAMESPACE" and body["docs_url"].endswith(
        "docs/home-assistant-naming.md"
    )
    assert body["preview"]["prefix"] == "fwh_mb" and body["preview"]["gateways"]
    status = (await client.get("/api/mqtt/status")).json()
    assert status["publishing_held"] is True


async def test_preview_of_a_candidate_and_a_refused_one(client):
    body = (await client.get("/api/setup/ha-naming", params={"prefix": "fwh_home"})).json()
    assert body["preview"]["prefix"] == "fwh_home" and body["preview"]["error"] is None
    assert body["preview"]["gateways"][0]["topic_root"].startswith("fwh_home/")
    body = (await client.get("/api/setup/ha-naming", params={"prefix": "fwhai"})).json()
    assert "reserved" in body["preview"]["error"] and body["preview"]["gateways"] == []


async def test_detect_without_a_broker_says_why(client):
    body = (await client.get("/api/setup/ha-naming", params={"detect": "true"})).json()
    assert body["previous_install"]["checked"] is False
    assert body["previous_install"]["error"]


async def test_api_choice_flow(client):
    r = await client.post("/api/setup/ha-naming", json={"topic_prefix": "fwh_mb"})
    assert r.status_code == 422 and "permanent" in r.json()["detail"]
    r = await client.post(
        "/api/setup/ha-naming", json={"topic_prefix": "fwh_lb", "acknowledged": True}
    )
    assert r.status_code == 422 and "reserved" in r.json()["detail"]
    assert app.state.mqtt_publisher.held

    r = await client.post(
        "/api/setup/ha-naming", json={"topic_prefix": "fwh_mb", "acknowledged": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "chosen" and r.json()["naming"]["pending"] is False
    assert not app.state.mqtt_publisher.held
    assert (await client.get("/api/setup/state")).json()["ha_naming_pending"] is False

    # Once published, the wizard can't change it: Settings does.
    await set_app_config(app.state.db, ns.PUBLISHED_KEY, "1")
    r = await client.post(
        "/api/setup/ha-naming", json={"topic_prefix": "franklinwh", "acknowledged": True}
    )
    assert r.status_code == 409 and "Change namespace" in r.json()["detail"]


async def test_env_var_app_publishes_without_waiting(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1")
    monkeypatch.setenv(ns.NAMESPACE_ENV, "fwh_mb")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    async with app.router.lifespan_context(app):
        assert not app.state.mqtt_publisher.held
        assert app.state.mqtt_publisher.topic_prefix == "fwh_mb"


async def test_existing_install_app_publishes_immediately_on_franklinwh(tmp_path, monkeypatch):
    """Regression for the live bridge: an install with a gateway on file."""
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1")
    monkeypatch.delenv(ns.NAMESPACE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    conn = await init_db(tmp_path / "data" / "bridge.db")
    await _add_gateway(conn, host="192.0.2.10", serial=SERIAL)
    await conn.close()
    async with app.router.lifespan_context(app):
        assert not app.state.mqtt_publisher.held
        assert app.state.mqtt_publisher.topic_prefix == "franklinwh"
        assert not await ns.is_pending(app.state.db)
