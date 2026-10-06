"""HA entity access auto-configured through the Supervisor (add-on installs).

Running as an add-on, the HA the bridge runs under is reachable through the
Supervisor proxy with the add-on's own token, so it becomes an entity source
with no URL or long-lived token to set up. Everywhere else this is inert.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api.ha_api import router as ha_router
from franklinwh_bridge.config.supervisor import (
    LOCAL_HA_ID,
    SUPERVISOR_CORE_URL,
    ensure_local_ha_instance,
    is_supervisor_instance,
)
from franklinwh_bridge.gateway.ha import HaInstance, HaRegistry
from franklinwh_bridge.store.db import create_ha_instance, get_ha_instance, init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "ha_sup.db")
    yield conn
    await conn.close()


@pytest.fixture
def addon_env(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token-1")
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)


@pytest.fixture
async def client(db):
    app = FastAPI()
    app.include_router(ha_router)
    app.state.db = db
    app.state.ha_registry = HaRegistry(db)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_inert_without_a_supervisor_token(db, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    assert await ensure_local_ha_instance(db) is False
    assert await get_ha_instance(db, LOCAL_HA_ID) is None


async def test_creates_the_local_instance_once(db, addon_env):
    assert await ensure_local_ha_instance(db) is True
    row = await get_ha_instance(db, LOCAL_HA_ID)
    assert row["base_url"] == SUPERVISOR_CORE_URL
    assert row["token"] is None  # never stored: it changes every add-on start
    assert row["is_default"]
    assert row["enabled"]
    assert is_supervisor_instance(row)

    # A user who disabled it keeps it disabled across restarts.
    await db.execute("UPDATE ha_instances SET enabled = 0 WHERE id = ?", (LOCAL_HA_ID,))
    await db.commit()
    assert await ensure_local_ha_instance(db) is False
    assert not (await get_ha_instance(db, LOCAL_HA_ID))["enabled"]


async def test_does_not_steal_an_existing_default(db, addon_env):
    await create_ha_instance(db, "Holiday house", "http://ha2:8123", token="t", is_default=True)
    await ensure_local_ha_instance(db)
    assert not (await get_ha_instance(db, LOCAL_HA_ID))["is_default"]


def test_instance_uses_the_supervisor_token_at_runtime(addon_env):
    inst = HaInstance({"id": LOCAL_HA_ID, "name": "x", "base_url": SUPERVISOR_CORE_URL})
    assert inst.token == "sup-token-1"
    assert inst.ws_url == "ws://supervisor/core/api/websocket"

    # Any other instance is untouched — the add-on token never leaks to it.
    other = HaInstance({"id": "ha_1", "name": "y", "base_url": "http://ha2:8123"})
    assert other.token is None


@respx.mock
async def test_refresh_through_the_supervisor_proxy(addon_env):
    route = respx.get(f"{SUPERVISOR_CORE_URL}/api/states").mock(
        return_value=httpx.Response(200, json=[{"entity_id": "sensor.a", "state": "1"}])
    )
    inst = HaInstance({"id": LOCAL_HA_ID, "name": "x", "base_url": SUPERVISOR_CORE_URL})
    await inst.refresh()
    assert inst.connected
    assert route.calls.last.request.headers["Authorization"] == "Bearer sup-token-1"


async def test_api_marks_it_managed_and_protects_it(client, db, addon_env):
    await ensure_local_ha_instance(db)

    rows = (await client.get("/api/ha/instances")).json()
    local = next(r for r in rows if r["id"] == LOCAL_HA_ID)
    assert local["managed"] == "supervisor"
    assert "token" not in local

    # URL and token are the Supervisor's, not the user's.
    resp = await client.patch(f"/api/ha/instances/{LOCAL_HA_ID}", json={"base_url": "http://x"})
    assert resp.status_code == 400
    resp = await client.patch(f"/api/ha/instances/{LOCAL_HA_ID}", json={"token": "abc"})
    assert resp.status_code == 400

    # Name and enabled are still the user's to change.
    resp = await client.patch(
        f"/api/ha/instances/{LOCAL_HA_ID}", json={"name": "Home", "enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Home"

    # Deleting would only bring it back on the next start.
    assert (await client.delete(f"/api/ha/instances/{LOCAL_HA_ID}")).status_code == 400

    # A user-added instance is unaffected by any of this.
    resp = await client.post(
        "/api/ha/instances", json={"name": "Other", "base_url": "http://ha2:8123", "token": "t"}
    )
    other = resp.json()
    assert other["managed"] is None
    assert (await client.delete(f"/api/ha/instances/{other['id']}")).status_code == 200
