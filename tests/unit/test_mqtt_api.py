"""Tests for MQTT administration API endpoints and DB config CRUD."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.db import get_mqtt_config, init_db, set_mqtt_config


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


# --- DB config CRUD ---

async def test_get_mqtt_config_defaults(tmp_path):
    db = await init_db(tmp_path / "test.db")
    try:
        config = await get_mqtt_config(db)
        assert config["host"] == "localhost"
        assert config["port"] == 1883
        assert config["username"] is None
        assert config["password"] is None
        assert config["tls_mode"] == "off"
        assert config["enabled"] is True
        assert config["client_id"] == "franklinwh_bridge"
        assert config["qos"] == 0
        assert config["retain_discovery"] is True
        assert config["topic_prefix"] == "franklinwh"
        assert config["discovery_prefix"] == "homeassistant"
    finally:
        await db.close()


async def test_set_mqtt_config(tmp_path):
    db = await init_db(tmp_path / "test.db")
    try:
        result = await set_mqtt_config(db, {
            "host": "mosquitto.local",
            "port": 8883,
            "client_id": "my_bridge",
        })
        assert result["host"] == "mosquitto.local"
        assert result["port"] == 8883
        assert result["client_id"] == "my_bridge"
        assert result["topic_prefix"] == "franklinwh"

        reloaded = await get_mqtt_config(db)
        assert reloaded["host"] == "mosquitto.local"
    finally:
        await db.close()


async def test_set_mqtt_config_boolean_fields(tmp_path):
    db = await init_db(tmp_path / "test.db")
    try:
        result = await set_mqtt_config(db, {"enabled": False, "retain_discovery": False})
        assert result["enabled"] is False
        assert result["retain_discovery"] is False

        result = await set_mqtt_config(db, {"enabled": True})
        assert result["enabled"] is True
        assert result["retain_discovery"] is False
    finally:
        await db.close()


async def test_set_mqtt_config_ignores_unknown_keys(tmp_path):
    db = await init_db(tmp_path / "test.db")
    try:
        result = await set_mqtt_config(db, {"host": "new", "bogus_key": "ignored"})
        assert result["host"] == "new"
    finally:
        await db.close()


# --- API endpoints ---

async def test_get_mqtt_config_api(client):
    resp = await client.get("/api/mqtt/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["host"] == "localhost"
    assert data["port"] == 1883
    assert data["enabled"] is True
    assert data["topic_prefix"] == "franklinwh"


async def test_patch_mqtt_config_api(client):
    resp = await client.patch("/api/mqtt/config", json={
        "host": "broker.local",
        "port": 8883,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["host"] == "broker.local"
    assert data["port"] == 8883

    resp = await client.get("/api/mqtt/config")
    assert resp.json()["host"] == "broker.local"


async def test_patch_mqtt_config_password_redacted(client):
    resp = await client.patch("/api/mqtt/config", json={
        "username": "admin",
        "password": "secret123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "admin"
    assert data["password"] == "***"


async def test_patch_mqtt_config_empty_body(client):
    resp = await client.patch("/api/mqtt/config", json={})
    assert resp.status_code == 400


async def test_patch_mqtt_config_validation(client):
    resp = await client.patch("/api/mqtt/config", json={"port": 99999})
    assert resp.status_code == 422

    resp = await client.patch("/api/mqtt/config", json={"qos": 5})
    assert resp.status_code == 422

    resp = await client.patch("/api/mqtt/config", json={"tls_mode": "invalid"})
    assert resp.status_code == 422


async def test_mqtt_status_api(client):
    resp = await client.get("/api/mqtt/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "connected" in data
    assert "entity_count" in data


async def test_mqtt_reconnect_api(client):
    resp = await client.post("/api/mqtt/reconnect")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reconnecting"


async def test_mqtt_publish_api(client):
    resp = await client.post("/api/mqtt/publish")
    assert resp.status_code == 200
    assert resp.json()["status"] == "discovery_republish_queued"


async def test_mqtt_detect_broker_dev_mode(client):
    resp = await client.get("/api/mqtt/detect-broker")
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is False
    assert data["source"] == "manual"


async def test_mqtt_topics_no_device_info(client):
    resp = await client.get("/api/mqtt/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["topics"] == [] or "note" in data


async def test_mqtt_topics_with_device_info(client):
    publisher = app.state.mqtt_publisher
    publisher.set_device_info(DeviceInfo(serial="10060006A02F00000001"))

    resp = await client.get("/api/mqtt/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["short_id"] == "00000001"
    assert data["entity_count"] > 20
    topics = data["topics"]
    slugs = {t["slug"] for t in topics}
    assert "battery_soc" in slugs
    assert "battery_power_kw" in slugs


async def test_mqtt_test_connection_api(client):
    resp = await client.post("/api/mqtt/test")
    assert resp.status_code == 200
    data = resp.json()
    assert "success" in data


# --- Publisher from_db_config ---

def test_publisher_from_db_config():
    config = {
        "host": "broker.io",
        "port": 8883,
        "username": "user",
        "password": "pass",
        "client_id": "test_bridge",
        "qos": 1,
        "topic_prefix": "fwh",
        "discovery_prefix": "ha",
    }
    pub = MqttPublisher.from_db_config(config, gateway_id="gw1")
    assert pub._host == "broker.io"
    assert pub._port == 8883
    assert pub._client_id == "test_bridge"
    assert pub._default_qos == 1
    assert pub._topic_prefix == "fwh"
    assert pub._discovery_prefix == "ha"


# --- Publisher reconfigure ---

async def test_publisher_reconfigure():
    pub = MqttPublisher(host="old.host")
    assert pub._host == "old.host"

    await pub.reconfigure({"host": "new.host", "port": 9999})
    assert pub._host == "new.host"
    assert pub._port == 9999


# --- Status in health component ---

async def test_status_includes_mqtt(client):
    resp = await client.get("/api/status")
    data = resp.json()
    mqtt_state = data["components"]["mqtt"]
    assert "connected" in mqtt_state
    assert "messages_sent" in mqtt_state
