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
        ac.app = app  # let tests reach app.state.ha_registry to seed entity caches
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


async def _make_instance_with_cache(client):
    """Create an instance via the API, then seed its live cache directly so the
    browser has entities to return (no reachable HA in tests)."""
    created = (
        await client.post("/api/ha/instances", json={"name": "Home", "base_url": "http://h:8123"})
    ).json()
    inst = client.app.state.ha_registry._instances[created["id"]]
    inst._states = {
        "sensor.amber_price": {
            "state": "31.2",
            "attributes": {"unit_of_measurement": "c/kWh", "friendly_name": "Amber Price"},
        },
        "binary_sensor.grid": {"state": "on", "attributes": {"friendly_name": "Grid OK"}},
        "light.lamp": {"state": "off", "attributes": {"friendly_name": "Lamp"}},
    }
    return created["id"]


async def test_browse_entities_shape_and_filters(client):
    iid = await _make_instance_with_cache(client)

    # unfiltered: all 3 entities, none exposed yet
    body = (await client.get("/api/ha/entities")).json()
    assert body["total"] == 3
    assert body["exposed_count"] == 0
    assert {e["entity_id"] for e in body["entities"]} == {
        "sensor.amber_price",
        "binary_sensor.grid",
        "light.lamp",
    }
    row = next(e for e in body["entities"] if e["entity_id"] == "sensor.amber_price")
    assert row["domain"] == "sensor" and row["value"] == 31.2 and row["exposed"] is False

    assert body["grand_total"] == 3
    # domain filter narrows `total` but grand_total (unfiltered) stays 3
    light = (await client.get("/api/ha/entities?domain=light")).json()
    assert light["total"] == 1 and light["grand_total"] == 3
    # search filter (matches friendly name)
    assert (await client.get("/api/ha/entities?search=amber")).json()["total"] == 1
    # instance filter + domains endpoint
    assert (await client.get(f"/api/ha/entities?instance={iid}")).json()["total"] == 3
    assert (await client.get("/api/ha/domains")).json() == ["binary_sensor", "light", "sensor"]


async def test_search_terms_are_ored(client):
    """Comma-separated search terms match as OR — the topic presets rely on it,
    since the same concept is named differently by every integration."""
    await _make_instance_with_cache(client)

    # Two topics that share no single substring still come back together.
    both = (await client.get("/api/ha/entities?search=amber,lamp")).json()
    assert both["total"] == 2
    assert {e["entity_id"] for e in both["entities"]} == {"sensor.amber_price", "light.lamp"}

    # Whitespace and empty segments are tolerated; a term matching nothing is a no-op.
    assert (await client.get("/api/ha/entities?search= amber , ,nomatch")).json()["total"] == 1
    # A plain single term behaves exactly as before (no regression).
    assert (await client.get("/api/ha/entities?search=amber")).json()["total"] == 1
    # entity_id matches too, not just friendly names.
    assert (await client.get("/api/ha/entities?search=binary_sensor.,light.")).json()["total"] == 2


async def test_pagination(client):
    await _make_instance_with_cache(client)
    p1 = (await client.get("/api/ha/entities?page=1&page_size=2")).json()
    assert p1["total"] == 3 and len(p1["entities"]) == 2 and p1["page"] == 1
    p2 = (await client.get("/api/ha/entities?page=2&page_size=2")).json()
    assert len(p2["entities"]) == 1  # remainder


async def test_expose_toggle_persists_and_filters(client):
    iid = await _make_instance_with_cache(client)
    # expose one entity
    resp = await client.post(
        "/api/ha/entities/expose",
        json={"instance_id": iid, "entity_id": "sensor.amber_price", "exposed": True},
    )
    assert resp.status_code == 200 and resp.json()["exposed"] is True

    body = (await client.get("/api/ha/entities?exposed=true")).json()
    assert body["total"] == 1
    assert body["entities"][0]["entity_id"] == "sensor.amber_price"
    assert (await client.get("/api/ha/entities")).json()["exposed_count"] == 1

    # it now flows into the sensor namespace (values)
    assert f"ha:{iid}:sensor.amber_price" in client.app.state.ha_registry.entity_values()

    # un-expose
    await client.post(
        "/api/ha/entities/expose",
        json={"instance_id": iid, "entity_id": "sensor.amber_price", "exposed": False},
    )
    assert (await client.get("/api/ha/entities?exposed=true")).json()["total"] == 0
