"""Add-on started before Mosquitto: wait for the broker, then connect by itself.

The manifest declares ``mqtt:want`` (not ``need``), so the add-on installs and
runs with no broker. It must then neither retry localhost forever nor need a
restart once Mosquitto is installed.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

import franklinwh_bridge.main as main_mod
from franklinwh_bridge.main import app

BROKER = {"host": "core-mosquitto", "port": 1883, "username": "addons",
          "password": "pw", "tls": False}


@pytest.fixture
async def addon_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    broker: dict = {"value": None}

    async def fake_discover(*, quiet: bool = False):
        return broker["value"]

    monkeypatch.setattr(main_mod, "discover_mqtt", fake_discover)
    monkeypatch.setattr(main_mod, "MQTT_WATCH_INTERVAL_S", 0.05)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        ac.broker = broker
        yield ac


async def test_waits_for_a_broker_then_configures_itself(addon_client):
    client = addon_client

    health = (await client.get("/api/health")).json()
    assert health["mqtt_broker"] == {"source": "none", "waiting_for_broker": True}
    publisher = app.state.mqtt_publisher
    # Not hammering localhost while there is nothing to connect to.
    assert publisher._task is None

    # Mosquitto gets installed: the Supervisor now reports an MQTT service.
    client.broker["value"] = BROKER
    for _ in range(100):
        if not app.state.mqtt_broker["waiting_for_broker"]:
            break
        await asyncio.sleep(0.02)

    health = (await client.get("/api/health")).json()
    assert health["mqtt_broker"] == {"source": "supervisor", "waiting_for_broker": False}
    assert publisher._host == "core-mosquitto"
    assert publisher._username == "addons"
    assert publisher._task is not None  # connection loop started, no restart


async def test_not_waiting_outside_an_addon(tmp_path, monkeypatch):
    """Docker/dev: no Supervisor, so the configured broker is used as-is."""
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        health = (await ac.get("/api/health")).json()
        assert health["mqtt_broker"] == {"source": "configured", "waiting_for_broker": False}
