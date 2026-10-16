"""Self-service password change and the setup wizard's accounts check."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api.auth import SUPERVISOR_INGRESS_IP
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.setup_api import router as setup_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import auth_sessions
from franklinwh_bridge.store.db import create_user, init_db


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    db = await init_db(tmp_path / "a.db")
    await create_user(db, "admin", hash_password("admin"), role="admin")      # compose default
    await create_user(db, "bob", hash_password("bob"), role="viewer")         # = username
    await create_user(db, "carol", hash_password("Str0ng-pass"), role="user")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = db
    app.state.config = SimpleNamespace(environment="docker")
    app.include_router(auth_router)
    app.include_router(setup_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    clients = []

    def new_client():
        c = AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield SimpleNamespace(db=db, app=app, new_client=new_client)
    for c in clients:
        await c.aclose()
    await db.close()


async def _login(c, u, p):
    r = await c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r


async def test_accounts_flags_guessable_passwords(env):
    c = env.new_client()
    await _login(c, "admin", "admin")
    d = (await c.get("/api/setup/accounts")).json()
    assert d["applies"] is True
    assert d["you"] == {"username": "admin", "role": "admin", "weak_password": True}
    weak = {a["username"]: a["weak_password"] for a in d["accounts"]}
    assert weak == {"admin": True, "bob": True, "carol": False}


async def test_accounts_is_admin_only(env):
    c = env.new_client()
    await _login(c, "carol", "Str0ng-pass")
    assert (await c.get("/api/setup/accounts")).status_code == 403


async def test_accounts_does_not_apply_under_ingress(env):
    env.app.state.config = SimpleNamespace(environment="ha_addon")
    transport = ASGITransport(app=env.app, client=(SUPERVISOR_INGRESS_IP, 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/setup/accounts", headers={"X-Ingress-Path": "/api/hassio_ingress/t"})
    assert r.json() == {"applies": False, "you": None, "accounts": []}


async def test_change_own_password_ends_other_logins_keeps_this_one(env):
    here, other = env.new_client(), env.new_client()
    await _login(here, "admin", "admin")
    await _login(other, "admin", "admin")

    r = await here.post("/api/auth/password",
                        json={"current_password": "admin", "new_password": "N3w-long-pass"})
    assert r.status_code == 200 and r.json()["other_logins_ended"] == 1

    assert (await here.get("/api/setup/accounts")).json()["you"]["weak_password"] is False
    d = (await other.get("/api/setup/accounts")).json()["detail"]
    assert d["reason"] == "password_changed" and d["ended_by"] == "admin"
    await _login(env.new_client(), "admin", "N3w-long-pass")
    events = {e["event"] for e in await auth_sessions.list_events(env.db)}
    assert "password_changed" in events


@pytest.mark.parametrize(
    ("current", "new", "status", "msg"),
    [
        ("wrong", "N3w-long-pass", 400, "current password is wrong"),
        ("admin", "short", 422, None),
        ("admin", "password", 400, "common default"),
        ("admin", "admin", 422, None),
    ],
)
async def test_change_own_password_rejections(env, current, new, status, msg):
    c = env.new_client()
    await _login(c, "admin", "admin")
    r = await c.post("/api/auth/password",
                     json={"current_password": current, "new_password": new})
    assert r.status_code == status, r.text
    if msg:
        assert msg in r.json()["detail"]


async def test_new_password_equal_to_username_refused(env):
    c = env.new_client()
    await _login(c, "carol", "Str0ng-pass")
    await env.db.execute("UPDATE users SET username = 'carolcarol' WHERE username = 'carol'")
    await env.db.commit()
    r = await c.post("/api/auth/password",
                     json={"current_password": "Str0ng-pass", "new_password": "CarolCarol"})
    assert r.status_code == 400 and "username" in r.json()["detail"]


async def test_change_password_requires_login(env):
    r = await env.new_client().post("/api/auth/password",
                                    json={"current_password": "x", "new_password": "N3w-long-pass"})
    assert r.status_code == 401
