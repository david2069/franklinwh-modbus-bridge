"""Tests for REST API gateway routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.modbus.sample import Sample

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "sunspec_sample.json"


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


async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "uptime_s" in data


async def test_status(client):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "components" in data
    assert "poller" in data["components"]
    assert "mqtt" in data["components"]


async def test_connectivity_endpoint(client):
    resp = await client.get("/api/health/connectivity")
    assert resp.status_code == 200
    data = resp.json()
    assert "connected" in data
    assert "gateways" in data
    assert "recent_outages" in data


async def test_config_crud(client):
    resp = await client.put("/api/config/theme", json={"value": "dark"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "dark"

    resp = await client.get("/api/config/theme")
    assert resp.status_code == 200
    assert resp.json()["value"] == "dark"

    resp = await client.put("/api/config/theme", json={"value": "light"})
    assert resp.json()["value"] == "light"


async def test_config_not_found(client):
    resp = await client.get("/api/config/nonexistent")
    assert resp.status_code == 404


async def test_config_all(client):
    await client.put("/api/config/k1", json={"value": "v1"})
    await client.put("/api/config/k2", json={"value": "v2"})
    resp = await client.get("/api/config/all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["k1"] == "v1"
    assert data["k2"] == "v2"


async def test_models_empty(client):
    resp = await client.get("/api/models")
    assert resp.status_code == 200
    assert resp.json()["models"] == []


async def test_models_after_capture(client):
    device_info = json.loads(FIXTURE_PATH.read_text())

    from franklinwh_bridge.modbus.catalog import capture_catalog

    db = app.state.db
    gateway_id = app.state.gateway_id
    await capture_catalog(device_info, db, gateway_id)

    resp = await client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()["models"]
    assert len(models) == 17
    model_ids = {m["model_id"] for m in models}
    assert model_ids == {
        1, 502, 701, 702, 703, 704, 705, 706, 707, 708,
        709, 710, 711, 712, 713, 714, 715,
    }


async def test_points_empty(client):
    resp = await client.get("/api/points")
    assert resp.status_code == 200
    data = resp.json()
    assert data["points"] == {}
    assert data["ts"] is None


async def test_points_after_publish(client):
    sample = Sample.now("default", {"soc": 85, "power": -1200})
    await app.state.sample_bus.publish(sample)

    resp = await client.get("/api/points")
    assert resp.status_code == 200
    data = resp.json()
    assert data["points"]["soc"] == 85
    assert data["quality"] == "ok"


async def test_single_point(client):
    sample = Sample.now("default", {"soc": 85, "power": -1200})
    await app.state.sample_bus.publish(sample)

    resp = await client.get("/api/points/soc")
    assert resp.status_code == 200
    assert resp.json()["value"] == 85


async def test_single_point_not_found(client):
    resp = await client.get("/api/points/nonexistent")
    assert resp.status_code == 404


async def test_logs(client):
    resp = await client.get("/api/logs")
    assert resp.status_code == 200
    assert "logs" in resp.json()


async def test_refresh_models_no_reader(client):
    resp = await client.post("/api/models/refresh")
    assert resp.status_code == 503
