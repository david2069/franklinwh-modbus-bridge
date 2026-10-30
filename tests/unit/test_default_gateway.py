"""The designated default gateway (#296 item 43): a stored mark, listed first,
moved by Make default, passed on when the default is deleted. The row with ID
'default' can't be deleted yet (D1) and is refused before anything stops."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.store.db import (
    create_gateway,
    delete_gateway,
    get_gateways,
    init_db,
    set_default_gateway,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "t.db")
    yield conn
    await conn.close()


async def _order(db):
    return [(g["id"], g["is_default"]) for g in await get_gateways(db)]


async def test_first_gateway_is_the_default_when_none_is_marked(db):
    await create_gateway(db, "a", "A", "192.0.2.1")
    await create_gateway(db, "b", "B", "192.0.2.2")
    assert await _order(db) == [("a", 1), ("b", 0)]


async def test_make_default_moves_the_mark_and_lists_it_first(db):
    for gid in ("a", "b", "c"):
        await create_gateway(db, gid, gid.upper(), "192.0.2.1")
    assert await set_default_gateway(db, "c")
    assert await _order(db) == [("c", 1), ("a", 0), ("b", 0)]
    assert await set_default_gateway(db, "b")
    assert await _order(db) == [("b", 1), ("a", 0), ("c", 0)]
    assert not await set_default_gateway(db, "nope")


async def test_deleting_the_default_makes_the_next_one_default(db):
    for gid in ("a", "b", "c"):
        await create_gateway(db, gid, gid.upper(), "192.0.2.1")
    await set_default_gateway(db, "b")
    assert await delete_gateway(db, "b")
    rows = await _order(db)
    assert rows == [("a", 1), ("c", 0)]
    # Stored, not just reported: the mark is on the row.
    async with db.execute("SELECT id FROM gateways WHERE is_default = 1") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["a"]


async def test_deleting_another_gateway_keeps_the_default(db):
    for gid in ("a", "b"):
        await create_gateway(db, gid, gid.upper(), "192.0.2.1")
    await set_default_gateway(db, "b")
    assert await delete_gateway(db, "a")
    assert await _order(db) == [("b", 1)]


async def test_the_default_id_row_cannot_be_deleted(db):
    await create_gateway(db, "default", "aGate", "")
    with pytest.raises(ValueError, match="can't be deleted yet"):
        await delete_gateway(db, "default")


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


async def test_api_make_default_and_delete_guard(client):
    """POST /default designates; DELETE of 'default' is refused before it stops."""
    resp = await client.post("/api/gateways", json={
        "gateway_id": "gw2", "name": "Gateway 2", "host": "192.0.2.2",
    })
    assert resp.status_code == 201
    first = (await client.get("/api/gateways")).json()["gateways"]
    assert first[0]["id"] == "default" and first[0]["is_default"]

    resp = await client.post("/api/gateways/gw2/default")
    assert resp.status_code == 200 and resp.json() == {"default": "gw2"}
    rows = (await client.get("/api/gateways")).json()["gateways"]
    pairs = [(g["id"], bool(g["is_default"])) for g in rows][:2]
    assert pairs == [("gw2", True), ("default", False)]

    assert (await client.post("/api/gateways/nope/default")).status_code == 404

    # Refused before anything is stopped or cleared (it used to stop the
    # gateway and clear its samples, then answer 403).
    registry, agg = app.state.registry, app.state.site_aggregator
    calls = []

    async def _stop(gid):
        calls.append(("stop", gid))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(registry, "get", lambda gid: object())
        mp.setattr(registry, "stop_gateway", _stop)
        mp.setattr(agg, "clear_gateway", lambda gid: calls.append(("clear", gid)))
        resp = await client.delete("/api/gateways/default")
    assert resp.status_code == 403
    assert "can't be deleted yet" in resp.json()["detail"]
    assert calls == []

    # Deleting the designated default hands the mark to the next gateway.
    assert (await client.delete("/api/gateways/gw2")).status_code == 200
    rows = (await client.get("/api/gateways")).json()["gateways"]
    assert rows[0]["id"] == "default" and rows[0]["is_default"]
