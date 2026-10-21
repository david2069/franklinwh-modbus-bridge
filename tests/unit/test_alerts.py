"""Critical alerts: the stored record and its REST API (#324, shared spec with DC #271)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.alerts_api import router as alerts_router
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import alerts
from franklinwh_bridge.store.db import create_user, init_db

T0 = 1_792_536_000.0


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "alerts.db")
    yield conn
    await conn.close()


async def test_event_open_until_acked_and_repeats_merge(db):
    a = await alerts.raise_event(db, "COMMAND_REFUSED", "exception 3", gateway_id="gw", now=T0)
    b = await alerts.raise_event(db, "COMMAND_REFUSED", "exception 3 again",
                                 gateway_id="gw", now=T0 + 60)
    assert a == b
    [row] = await alerts.list_alerts(db, open_only=True)
    assert row["count"] == 2 and row["last_ts"] == T0 + 60 and row["severity"] == "critical"

    assert await alerts.ack(db, a, "user:root", now=T0 + 90)
    assert not await alerts.ack(db, a, "user:root")  # once only
    assert await alerts.list_alerts(db, open_only=True) == []

    # After the window, or once acknowledged, a repeat is a new alert.
    c = await alerts.raise_event(db, "COMMAND_REFUSED", "later", gateway_id="gw", now=T0 + 100)
    assert c != a


async def test_condition_lifecycle_and_one_open_per_code(db):
    a = await alerts.open_condition(db, "DATA_MISSING_PARTIAL", "155xx missing",
                                    gateway_id="gw", now=T0)
    assert a == await alerts.open_condition(db, "DATA_MISSING_PARTIAL", "still",
                                            gateway_id="gw", now=T0 + 30)
    await alerts.ack(db, a, "user:root", now=T0 + 40)
    # Acknowledged but still active: still open (the red bar stays).
    assert [r["id"] for r in await alerts.list_alerts(db, open_only=True)] == [a]
    assert await alerts.resolve_condition(db, "DATA_MISSING_PARTIAL", gateway_id="gw",
                                          now=T0 + 120) == a
    assert await alerts.list_alerts(db, open_only=True) == []
    # Resolved before anyone looked: open until acknowledged ("09:54–13:27, resolved").
    b = await alerts.open_condition(db, "GATEWAY_UNREACHABLE", "down", gateway_id="gw",
                                    now=T0 + 200)
    await alerts.resolve_condition(db, "GATEWAY_UNREACHABLE", gateway_id="gw", now=T0 + 300)
    [row] = await alerts.list_alerts(db, open_only=True)
    assert row["id"] == b and row["resolved_ts"] == T0 + 300


async def test_kind_is_fixed_per_code(db):
    with pytest.raises(ValueError):
        await alerts.raise_event(db, "GATEWAY_UNREACHABLE", "x")
    with pytest.raises(ValueError):
        await alerts.open_condition(db, "COMMAND_REFUSED", "x")
    with pytest.raises(ValueError):
        await alerts.raise_event(db, "NOT_A_CODE", "x")


async def test_summary_and_changed_ts(db):
    assert await alerts.summary(db) == {
        "level": "ok", "open": 0, "latest_id": None, "changed_ts": None}
    await alerts.raise_event(db, "EXTERNAL_SETPOINT", "app wrote it", now=T0)
    assert (await alerts.summary(db))["level"] == "warning"
    a = await alerts.raise_event(db, "COMMAND_REFUSED", "x", now=T0 + 1)
    s = await alerts.summary(db)
    assert s["level"] == "critical" and s["open"] == 2 and s["latest_id"] == a
    await alerts.ack(db, a, "user:root", now=T0 + 50)
    s2 = await alerts.summary(db)
    assert s2["changed_ts"] == T0 + 50 and s2["latest_id"] == a  # ack moves changed_ts


async def test_prune_keeps_open_alerts(db):
    old = T0 - alerts.RETENTION_S - 10
    a = await alerts.raise_event(db, "COMMAND_REFUSED", "acked", now=old)
    await alerts.ack(db, a, "user:root", now=old)
    keep = await alerts.raise_event(db, "SCHEDULE_FAILED", "never acked", now=old)
    assert await alerts.prune(db, now=T0) == 1
    assert [r["id"] for r in await alerts.list_alerts(db)] == [keep]


async def test_survives_a_restart(tmp_path):
    db = await init_db(tmp_path / "a.db")
    a = await alerts.raise_event(db, "COMMAND_REFUSED", "x", now=T0)
    await db.close()
    db = await init_db(tmp_path / "a.db")
    [row] = await alerts.list_alerts(db, open_only=True)
    assert row["id"] == a
    await db.close()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    conn = await init_db(tmp_path / "api.db")
    await create_user(conn, "root", hash_password("pw"), role="admin")
    await create_user(conn, "vee", hash_password("pw"), role="viewer")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = conn
    app.state.config = SimpleNamespace(environment="dev")
    app.include_router(auth_router)
    app.include_router(alerts_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.db = conn
        yield ac
    await conn.close()


async def _login(ac, u):
    assert (await ac.post("/api/auth/login", json={"username": u, "password": "pw"})
            ).status_code == 200


async def test_api_list_and_ack_roles(client):
    a = await alerts.raise_event(client.db, "COMMAND_REFUSED", "exception 3", gateway_id="gw")
    assert (await client.get("/api/alerts")).status_code == 401

    await _login(client, "vee")
    rows = (await client.get("/api/alerts?open=1")).json()
    assert [r["id"] for r in rows] == [a] and rows[0]["deliveries"] == []
    assert (await client.post(f"/api/alerts/{a}/ack")).status_code == 403  # viewers can't

    await _login(client, "root")
    r = await client.post(f"/api/alerts/{a}/ack")
    assert r.status_code == 200 and r.json()["acked_by"] == "user:root"
    assert (await client.post(f"/api/alerts/{a}/ack")).status_code == 404
    assert (await client.get("/api/alerts?open=1")).json() == []
