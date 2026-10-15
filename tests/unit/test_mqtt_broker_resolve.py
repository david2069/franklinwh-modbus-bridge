"""Where the MQTT broker settings come from, and Save & Reconnect applying them (#103).

Before: the environment's host always replaced the saved one — even an unset
MQTT_HOST's "localhost" default did — so a broker saved in Settings was never
used, and Reconnect restarted against the old broker.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.config.mqtt_broker import resolve_broker
from franklinwh_bridge.main import app

_STORED = {"host": "localhost", "port": 1883, "username": None, "password": None}


@pytest.fixture(autouse=True)
def _no_mqtt_env(monkeypatch):
    for var in ("MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


async def test_saved_broker_is_used_when_the_environment_sets_none():
    cfg, src = await resolve_broker({**_STORED, "host": "broker.lan", "port": 1884})
    assert (cfg["host"], cfg["port"]) == ("broker.lan", 1884)
    assert src["host"] == "settings"


async def test_environment_wins(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "mosquitto")
    monkeypatch.setenv("MQTT_PORT", "1885")
    cfg, src = await resolve_broker({**_STORED, "host": "broker.lan"})
    assert (cfg["host"], cfg["port"]) == ("mosquitto", 1885)
    assert src["host"] == src["port"] == "environment"


async def test_empty_environment_variables_are_unset(monkeypatch):
    """docker-compose passes MQTT_USERNAME= when it isn't configured."""
    monkeypatch.setenv("MQTT_USERNAME", "")
    cfg, src = await resolve_broker({**_STORED, "username": "saved"})
    assert cfg["username"] == "saved" and src["username"] == "settings"


async def test_supervisor_only_when_no_host_is_set():
    calls = []

    async def discover():
        calls.append(1)
        return {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "x"}

    cfg, src = await resolve_broker(dict(_STORED), discover)
    assert cfg["host"] == "core-mosquitto" and src["host"] == "supervisor"

    calls.clear()
    cfg, src = await resolve_broker({**_STORED, "host": "broker.lan"}, discover)
    assert calls == [] and cfg["host"] == "broker.lan"


async def test_nothing_set_falls_back_to_localhost():
    async def none():
        return None

    cfg, src = await resolve_broker(dict(_STORED), none)
    assert cfg["host"] == "localhost" and src["host"] == "default"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MQTT_PORT", "1")  # nothing listens: the publisher just retries
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_save_and_reconnect_applies_the_new_host(client):
    resp = await client.patch("/api/mqtt/config", json={"host": "broker.lan"})
    assert resp.status_code == 200, resp.text
    resp = await client.post("/api/mqtt/reconnect")
    assert resp.status_code == 200
    assert resp.json()["host"] == "broker.lan"
    assert app.state.mqtt_publisher._host == "broker.lan"
    cfg = (await client.get("/api/mqtt/config")).json()
    assert cfg["sources"]["host"] == "settings"
    await app.state.mqtt_publisher.stop()


async def test_a_field_the_environment_sets_cannot_be_changed(client):
    cfg = (await client.get("/api/mqtt/config")).json()
    assert cfg["env_vars"] == {"port": "MQTT_PORT"}
    resp = await client.patch("/api/mqtt/config", json={"port": 1999})
    assert resp.status_code == 409
    assert "MQTT_PORT" in resp.json()["detail"]
    # Re-sending the environment's own value (the form does) is accepted.
    assert (await client.patch("/api/mqtt/config", json={"port": 1, "qos": 0})).status_code == 200
