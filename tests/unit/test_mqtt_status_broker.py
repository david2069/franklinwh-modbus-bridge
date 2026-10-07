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
