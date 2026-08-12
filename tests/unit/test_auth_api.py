"""Auth routes + require_auth/require_role, against a self-contained app (so the
conftest auth-bypass — which targets the real main.app — does not apply here)."""

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import require_auth, require_role
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store.db import create_user, init_db, update_user


def _build_app(db, environment="dev"):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.state.db = db
    app.state.config = SimpleNamespace(environment=environment)
    app.include_router(auth_router)

    @app.get("/protected", dependencies=[Depends(require_auth)])
    async def _protected():
        return {"ok": True}

    @app.get("/admin-only", dependencies=[Depends(require_role("admin"))])
    async def _admin_only():
        return {"ok": True}

    return app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")  # allow http login in tests
    db = await init_db(tmp_path / "auth.db")
    await create_user(db, "alice", hash_password("pw"), role="admin")
    await create_user(db, "bob", hash_password("pw"), role="viewer")
    app = _build_app(db)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac._db = db
        yield ac
    await db.close()


async def test_protected_requires_login(client):
    assert (await client.get("/protected")).status_code == 401


async def test_login_logout_me_flow(client):
    assert (
        await client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    ).status_code == 401
    r = await client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "alice"
    assert "password_hash" not in r.json()["user"]  # never leaked
    assert "automations" in r.json()["capabilities"]
    # cookie session persists across requests
    assert (await client.get("/protected")).status_code == 200
    me = (await client.get("/api/auth/me")).json()
    assert me["user"]["username"] == "alice"
    await client.post("/api/auth/logout")
    assert (await client.get("/protected")).status_code == 401
    assert (await client.get("/api/auth/me")).json()["user"] is None


async def test_require_role_blocks_viewer(client):
    await client.post("/api/auth/login", json={"username": "bob", "password": "pw"})  # viewer
    assert (await client.get("/protected")).status_code == 200
    assert (await client.get("/admin-only")).status_code == 403  # viewer != admin


async def test_disabled_user_cannot_login(client):
    # disable alice, then login must fail
    from franklinwh_bridge.store.db import get_user_by_username

    alice = await get_user_by_username(client._db, "alice")
    await update_user(client._db, alice["id"], enabled=False)
    r = await client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert r.status_code == 401


async def test_login_refused_over_http_without_override(client, monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_AUTH", raising=False)
    r = await client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert r.status_code == 400  # HTTPS required


async def test_ingress_bypass_returns_admin(tmp_path):
    db = await init_db(tmp_path / "ing.db")
    try:
        app = _build_app(db, environment="ha_addon")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # no login, but ingress → synthetic admin, protected succeeds
            assert (await ac.get("/protected")).status_code == 200
            assert (await ac.get("/admin-only")).status_code == 200
    finally:
        await db.close()
