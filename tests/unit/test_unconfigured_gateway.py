"""The default gateway with no host: created, but never started or polled.

Before this, an install with no gateway configured got a "Default Gateway" at a
made-up 192.168.1.100 that it polled forever and could not delete.

Consolidated into few tests: each app lifespan boot is slow.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.store.db import (
    UNCONFIGURED_DESCRIPTION,
    gateway_is_unconfigured,
    init_db,
)


@pytest.fixture
async def unconfigured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("MODBUS_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


def test_gateway_is_unconfigured():
    assert gateway_is_unconfigured({"host": "", "mock": 0})
    assert gateway_is_unconfigured({"host": "  ", "mock": 0})
    assert gateway_is_unconfigured({"host": None, "mock": 0})
    assert not gateway_is_unconfigured({"host": "10.0.0.5", "mock": 0})
    # A mock needs no address.
    assert not gateway_is_unconfigured({"host": "", "mock": 1})
    assert not gateway_is_unconfigured(None)


async def test_unconfigured_default_is_not_started_until_given_a_host(unconfigured_client):
    client = unconfigured_client

    resp = await client.get("/api/gateways")
    gws = {g["id"]: g for g in resp.json()["gateways"]}
    default = gws["default"]
    assert default["host"] == ""
    assert default["description"] == UNCONFIGURED_DESCRIPTION
    assert default["health"] == "unconfigured"
    assert default["polling"] is False
    assert (await client.get("/api/gateways/default")).json()["health"] == "unconfigured"

    # Let the background bring-up run; it must not register the gateway.
    await asyncio.sleep(0.5)
    assert app.state.registry.get("default") is None

    # Starting it without an address is refused, not attempted.
    resp = await client.post("/api/gateways/default/start")
    assert resp.status_code == 400
    assert "no host" in resp.json()["detail"]

    # Setting the address starts it — no second switch to find.
    resp = await client.patch("/api/gateways/default", json={"host": "192.0.2.20"})
    assert resp.status_code == 200
    assert app.state.registry.get("default") is not None
    default = (await client.get("/api/gateways/default")).json()
    assert default["host"] == "192.0.2.20"
    assert default["health"] != "unconfigured"


async def test_unconfigured_default_still_allows_a_mock(unconfigured_client):
    """Exploring without hardware: add a mock alongside the unconfigured default."""
    client = unconfigured_client
    resp = await client.post(
        "/api/gateways", json={"gateway_id": "demo", "name": "Demo", "mock": True},
    )
    assert resp.status_code == 201
    assert app.state.registry.get("demo") is not None
    assert app.state.registry.get("default") is None
    # Registered with MQTT at once — not only after a restart. Without this the
    # mock's samples were dropped ("no registered MQTT device") and it never
    # appeared in Home Assistant.
    assert app.state.mqtt_publisher.get_device("demo") is not None


async def test_migration_48_unphantoms_only_the_placeholder_row(tmp_path):
    db_path = tmp_path / "m.db"
    db = await init_db(db_path)
    try:
        rows = [
            # The phantom: placeholder address, auto-created, never connected.
            ("default", "192.168.1.100", "Auto-created from environment config", None, None),
        ]
        await db.execute("DELETE FROM gateways")
        for gid, host, desc, serial, connected in rows:
            await db.execute(
                "INSERT INTO gateways (id, name, host, port, unit_id, enabled, created_at, "
                "description, serial, last_connected_at) VALUES (?, ?, ?, 502, 1, 1, 0, ?, ?, ?)",
                (gid, gid, host, desc, serial, connected),
            )
        await db.execute("DELETE FROM schema_version WHERE version = 48")
        await db.commit()
    finally:
        await db.close()

    db = await init_db(db_path)  # re-runs migration 48
    try:
        async with db.execute("SELECT host, description FROM gateways WHERE id='default'") as c:
            host, desc = await c.fetchone()
        assert host == ""
        assert desc == UNCONFIGURED_DESCRIPTION
    finally:
        await db.close()


async def test_migration_48_leaves_a_real_gateway_alone(tmp_path):
    """Same placeholder address, but it has connected: it's a real aGate."""
    db_path = tmp_path / "m.db"
    db = await init_db(db_path)
    try:
        await db.execute("DELETE FROM gateways")
        await db.execute(
            "INSERT INTO gateways (id, name, host, port, unit_id, enabled, created_at, "
            "description, serial, last_connected_at) VALUES "
            "('default', 'Default', '192.168.1.100', 502, 1, 1, 0, "
            "'Auto-created from environment config', 'AG123', 1700000000)",
        )
        await db.execute("DELETE FROM schema_version WHERE version = 48")
        await db.commit()
    finally:
        await db.close()

    db = await init_db(db_path)
    try:
        async with db.execute("SELECT host FROM gateways WHERE id='default'") as c:
            assert (await c.fetchone())[0] == "192.168.1.100"
    finally:
        await db.close()
