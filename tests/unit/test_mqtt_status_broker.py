"""/api/mqtt/status reports the broker in use, not just the stored config."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MQTT_HOST", "broker.example")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_status_reports_the_broker_in_use(client):
    body = (await client.get("/api/mqtt/status")).json()
    assert body["broker"]["host"] == "broker.example"
    assert body["broker"]["port"] == 1883
    assert body["broker"]["source"] == "configured"


@pytest.fixture
async def no_broker_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    for var in ("MQTT_HOST", "MQTT_PORT", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_no_broker_set_reports_none_and_does_not_connect(no_broker_client):
    """#414: with no broker set, the bridge said "configured", tried
    localhost:1883 and logged an ERROR on every retry. MQTT is optional."""
    health = (await no_broker_client.get("/api/health")).json()
    assert health["mqtt_broker"] == {"source": "none", "waiting_for_broker": False}
    assert app.state.mqtt_publisher._task is None or app.state.mqtt_publisher._task.done()
    # Save & Reconnect with still nothing set stays quiet.
    r = await no_broker_client.post("/api/mqtt/reconnect")
    assert r.json() == {"status": "no_broker"}
