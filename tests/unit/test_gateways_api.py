"""HTTP-level tests for the /api/gateways and /api/site REST endpoints.

Tests are consolidated to minimise app lifespan boot overhead (~40s each).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app


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


async def test_site_config_crud(client):
    """GET + PATCH /api/site — read and update site configuration."""
    # Read defaults
    resp = await client.get("/api/site")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "My Site"
    assert data["ac_service_type"] == 1

    # Update
    resp = await client.patch("/api/site", json={
        "name": "Test Site",
        "meter_number": "NMI123",
        "ac_service_type": 3,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Site"
    assert resp.json()["meter_number"] == "NMI123"
    assert resp.json()["ac_service_type"] == 3


async def test_gateway_list_and_crud(client):
    """Full gateway CRUD: list, create, get, update, delete."""
    # List — should have "default"
    resp = await client.get("/api/gateways")
    assert resp.status_code == 200
    gateways = resp.json()["gateways"]
    assert any(g["id"] == "default" for g in gateways)

    # Create a second gateway
    resp = await client.post("/api/gateways", json={
        "gateway_id": "gw2",
        "name": "Gateway 2",
        "host": "192.168.1.101",
        "port": 502,
        "description": "Phase 2",
    })
    assert resp.status_code == 201
    assert resp.json()["id"] == "gw2"

    # Get single
    resp = await client.get("/api/gateways/gw2")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Gateway 2"

    # Onboarded on add: the gateway is registered + polling immediately, so its
    # per-gateway points endpoint resolves (200) instead of 404 "not running".
    resp = await client.get("/api/gateways/gw2/points")
    assert resp.status_code == 200

    # Update
    resp = await client.patch("/api/gateways/gw2", json={
        "name": "Renamed GW2",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed GW2"

    # Duplicate → 409
    resp = await client.post("/api/gateways", json={
        "gateway_id": "gw2",
        "name": "Dup",
        "host": "10.0.0.1",
    })
    assert resp.status_code == 409

    # Delete
    resp = await client.delete("/api/gateways/gw2")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Confirm gone
    resp = await client.get("/api/gateways/gw2")
    assert resp.status_code == 404

    # Cannot delete default
    resp = await client.delete("/api/gateways/default")
    assert resp.status_code == 403

    # 404 for nonexistent
    resp = await client.get("/api/gateways/nope")
    assert resp.status_code == 404


async def test_site_status(client):
    """GET /api/site/status returns aggregated data."""
    resp = await client.get("/api/site/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "gateway_count" in data
    assert "points" in data
