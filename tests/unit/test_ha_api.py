"""REST tests for the multi-HA config surface (/api/ha/*).

Uses a minimal FastAPI app wired with just the ha_router + a real db and
HaRegistry — no full Bridge lifespan (so no aGate dependency, fast + hermetic).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api.ha_api import router as ha_router
from franklinwh_bridge.gateway.ha import HaRegistry
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def client(tmp_path):
    db = await init_db(tmp_path / "ha_api.db")
    app = FastAPI()
    app.include_router(ha_router)
    app.state.db = db
    app.state.ha_registry = HaRegistry(db)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await db.close()


async def test_crud_lifecycle_and_token_redaction(client):
    # create — token must NOT be echoed back, only has_token
    resp = await client.post(
        "/api/ha/instances",
        json={
            "name": "Home",
            "base_url": "http://ha1:8123/",
            "token": "secret",
            "is_default": True,
        },
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["id"].startswith("ha_")
    assert created["base_url"] == "http://ha1:8123"  # trailing slash stripped
    assert created["is_default"] is True
    assert "token" not in created
    assert created["has_token"] is True

    # list — carries status block from the registry
    resp = await client.get("/api/ha/instances")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Home"
    assert "token" not in rows[0]
    assert "status" in rows[0]

    hid = created["id"]

    # patch — omitted token is preserved (exclude_unset)
    resp = await client.patch(
        f"/api/ha/instances/{hid}", json={"name": "Renamed", "enabled": False}
    )
    assert resp.status_code == 200
    patched = resp.json()
    assert patched["name"] == "Renamed"
    assert patched["enabled"] is False
    assert patched["has_token"] is True  # token untouched

    # delete
    resp = await client.delete(f"/api/ha/instances/{hid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == hid
    assert (await client.get("/api/ha/instances")).json() == []


async def test_patch_and_delete_unknown_404(client):
    assert (await client.patch("/api/ha/instances/nope", json={"name": "x"})).status_code == 404
    assert (await client.delete("/api/ha/instances/nope")).status_code == 404


@respx.mock
async def test_test_connection_probe(client):
    respx.get("http://ha.local/api/states").mock(
        return_value=httpx.Response(200, json=[{"entity_id": "sensor.a", "state": "1"}])
    )
    resp = await client.post("/api/ha/test", json={"base_url": "http://ha.local", "token": "t"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["entity_count"] == 1
    assert body["last_error"] is None


@respx.mock
async def test_test_connection_failure(client):
    respx.get("http://ha.bad/api/states").mock(return_value=httpx.Response(401))
    resp = await client.post("/api/ha/test", json={"base_url": "http://ha.bad", "token": "x"})
    assert resp.json()["connected"] is False
    assert resp.json()["last_error"] is not None


async def test_entities_reflects_registry(client):
    # create + reload happens inside the POST handler; entities come from the
    # registry's live cache. With no reachable HA the catalog is empty (connection
    # fails), but the endpoint must still return a list, not error.
    await client.post("/api/ha/instances", json={"name": "H", "base_url": "http://127.0.0.1:1/"})
    resp = await client.get("/api/ha/entities")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
