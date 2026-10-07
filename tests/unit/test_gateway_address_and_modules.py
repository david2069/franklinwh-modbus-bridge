"""/api/gateway reads the configured gateway (not MODBUS_HOST); the Explorer's
catalog endpoints are per gateway; Explorer/Sequencer default off for new installs."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway.modules import MODULES
from franklinwh_bridge.main import app
from franklinwh_bridge.store.db import MIGRATIONS, get_app_config, init_db


@pytest.fixture
async def fresh_client(tmp_path, monkeypatch):
    """A fresh install, aGate configured in the UI rather than by MODBUS_HOST."""
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


async def test_gateway_address_comes_from_the_gateway_row(fresh_client):
    client = fresh_client
    resp = await client.patch("/api/gateways/default", json={"host": "127.0.0.1", "port": 9})
    assert resp.status_code == 200
    gw = (await client.get("/api/gateway")).json()
    assert (gw["host"], gw["port"], gw["gateway_id"]) == ("127.0.0.1", 9, "default")

    # A named gateway — e.g. a demo — and its catalog, independently.
    resp = await client.post("/api/gateways", json={"gateway_id": "demo", "name": "Demo", "mock": True})
    assert resp.status_code == 201
    demo = (await client.get("/api/gateway?gateway_id=demo")).json()
    assert demo["mock"] is True and demo["gateway_id"] == "demo"
    assert (await client.get("/api/gateway?gateway_id=nope")).status_code == 404
    test = (await client.get("/api/gateway/test?gateway_id=demo")).json()
    assert test["ok"] is False and "demo" in test["error"]

    models = (await client.get("/api/models?gateway_id=demo")).json()
    assert models["gateway_id"] == "demo"


async def test_explorer_and_sequencer_are_off_on_a_new_install(fresh_client):
    mods = {m["id"]: m for m in (await fresh_client.get("/api/modules")).json()["modules"]}
    assert mods["explorer"]["enabled"] is False
    assert mods["sequencer"]["enabled"] is False
    assert mods["logs"]["enabled"] is True


@pytest.mark.parametrize(("existing", "expected"), [
    (None, {"explorer": True, "sequencer": True}),                      # never toggled
    ('{"sequencer": false}', {"explorer": True, "sequencer": False}),  # choice kept
])
async def test_migration_51_keeps_existing_installs_unchanged(tmp_path, existing, expected):
    db = await init_db(tmp_path / "bridge.db")
    try:
        await db.execute("DELETE FROM app_config WHERE key = 'modules_enabled'")
        if existing is not None:
            await db.execute(
                "INSERT INTO app_config (key, value) VALUES ('modules_enabled', ?)", (existing,))
        await db.execute(
            "INSERT INTO gateways (id, name, host, created_at) VALUES ('default', 'aGate', '10.0.0.5', 0)")
        await db.commit()
        await db.executescript(MIGRATIONS[51])
        stored = json.loads(await get_app_config(db, "modules_enabled"))
        assert {k: stored[k] for k in expected} == expected
    finally:
        await db.close()


def test_new_install_defaults():
    defaults = {m["id"]: m["default_enabled"] for m in MODULES}
    assert defaults["explorer"] is False and defaults["sequencer"] is False
