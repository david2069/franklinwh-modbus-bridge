"""HTTP-level tests for the /api/groups REST endpoints.

Tests are grouped into a few large functions to share the expensive app
lifespan boot (~40s per fixture setup).  Each function exercises a
coherent slice of the API surface.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES


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


async def test_entity_catalog_and_list_groups(client):
    """GET /api/groups/entities and GET /api/groups — read-only listing."""
    # ── Entity catalog ──
    resp = await client.get("/api/groups/entities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == len(BRIDGE_ENTITIES)
    entities = data["entities"]
    assert len(entities) == len(BRIDGE_ENTITIES)

    # Spot-check structure
    first = entities[0]
    for key in ("slug", "name", "ha_type", "state_group", "is_control"):
        assert key in first, f"Missing key: {key}"

    # Has control entities
    controls = [e for e in entities if e["is_control"]]
    assert len(controls) >= 5

    # ── List groups ──
    resp = await client.get("/api/groups")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 7
    slugs = [g["slug"] for g in groups]
    assert "battery" in slugs
    assert "power" in slugs
    assert "control" in slugs

    # Structure
    grp = groups[0]
    for key in ("slug", "name", "enabled", "is_default", "member_count"):
        assert key in grp, f"Missing key: {key}"
    assert grp["member_count"] > 0

    # ── Get single group ──
    resp = await client.get("/api/groups/battery")
    assert resp.status_code == 200
    bat = resp.json()
    assert bat["slug"] == "battery"
    assert "members" in bat
    assert "battery_soc" in bat["members"]

    # 404 for nonexistent
    resp = await client.get("/api/groups/nonexistent")
    assert resp.status_code == 404

    # ── Disabled entities (none initially) ──
    resp = await client.get("/api/groups/disabled")
    assert resp.status_code == 200
    assert resp.json()["disabled_slugs"] == []


async def test_create_update_delete_group(client):
    """Full CRUD lifecycle: create → update → delete a custom group."""
    # ── Create ──
    resp = await client.post("/api/groups", json={
        "slug": "my_custom",
        "name": "My Custom Group",
        "description": "Testing",
        "members": ["battery_soc", "grid_power_kw"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["slug"] == "my_custom"
    assert data["name"] == "My Custom Group"
    assert data["is_default"] is False
    assert sorted(data["members"]) == ["battery_soc", "grid_power_kw"]

    # Duplicate → 409
    resp = await client.post("/api/groups", json={
        "slug": "my_custom", "name": "Dup",
    })
    assert resp.status_code == 409

    # Invalid slug → 422
    resp = await client.post("/api/groups", json={
        "slug": "UPPER", "name": "Bad",
    })
    assert resp.status_code == 422

    # Slug too short → 422
    resp = await client.post("/api/groups", json={
        "slug": "x", "name": "Short",
    })
    assert resp.status_code == 422

    # ── Update name ──
    resp = await client.patch("/api/groups/my_custom", json={
        "name": "Renamed Custom",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Custom"

    # Update enabled
    resp = await client.patch("/api/groups/my_custom", json={
        "enabled": False,
    })
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    # Patch nonexistent → 404
    resp = await client.patch("/api/groups/nonexistent", json={"name": "X"})
    assert resp.status_code == 404

    # ── Delete custom ──
    resp = await client.delete("/api/groups/my_custom")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Confirm gone
    resp = await client.get("/api/groups/my_custom")
    assert resp.status_code == 404

    # Delete default → 403
    resp = await client.delete("/api/groups/battery")
    assert resp.status_code == 403

    # Delete nonexistent → 404
    resp = await client.delete("/api/groups/nope")
    assert resp.status_code == 404


async def test_member_management(client):
    """Member CRUD: list, replace, add, remove."""
    # ── List members ──
    resp = await client.get("/api/groups/battery/members")
    assert resp.status_code == 200
    members = resp.json()["members"]
    assert "battery_soc" in members

    # 404 for nonexistent group
    resp = await client.get("/api/groups/nonexistent/members")
    assert resp.status_code == 404

    # ── Replace members ──
    resp = await client.put("/api/groups/battery/members", json={
        "members": ["battery_soc"],
    })
    assert resp.status_code == 200
    assert resp.json()["members"] == ["battery_soc"]

    # ── Add member ──
    resp = await client.post("/api/groups/battery/members", json={
        "entity_slug": "grid_power_kw",
    })
    assert resp.status_code == 200
    assert "grid_power_kw" in resp.json()["members"]

    # ── Remove member ──
    resp = await client.delete("/api/groups/battery/members/grid_power_kw")
    assert resp.status_code == 200
    assert "grid_power_kw" not in resp.json()["members"]


async def test_disable_group_and_roundtrip(client):
    """Disabling a group marks its exclusive entities as disabled; full round-trip."""
    # Disable battery → its entities become disabled
    await client.patch("/api/groups/battery", json={"enabled": False})
    resp = await client.get("/api/groups/disabled")
    disabled = resp.json()["disabled_slugs"]
    assert "battery_soc" in disabled
    assert "battery_power_kw" in disabled

    # Re-enable → entities no longer disabled
    await client.patch("/api/groups/battery", json={"enabled": True})
    resp = await client.get("/api/groups/disabled")
    assert "battery_soc" not in resp.json()["disabled_slugs"]

    # ── Round-trip: create custom, add members, disable, verify ──
    await client.post("/api/groups", json={
        "slug": "roundtrip_test",
        "name": "Round-Trip",
        "members": ["battery_soc"],
    })
    await client.post("/api/groups/roundtrip_test/members", json={
        "entity_slug": "grid_power_kw",
    })

    # Disable custom group — entities should NOT be disabled since they're
    # still in their default groups (which are enabled)
    await client.patch("/api/groups/roundtrip_test", json={"enabled": False})
    resp = await client.get("/api/groups/disabled")
    disabled = resp.json()["disabled_slugs"]
    assert "battery_soc" not in disabled  # still in 'battery' (enabled)
    assert "grid_power_kw" not in disabled  # still in 'power' (enabled)

    # Clean up
    resp = await client.delete("/api/groups/roundtrip_test")
    assert resp.json()["deleted"] is True
