"""Admin Users management API (own app; admin session; lockout guards)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.users_api import router as users_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store.db import create_user, init_db


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    db = await init_db(tmp_path / "users.db")
    await create_user(db, "root", hash_password("pw"), role="admin")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = db
    app.state.config = SimpleNamespace(environment="dev")
    app.include_router(auth_router)
    app.include_router(users_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await db.close()


async def _login(ac, u="root", p="pw"):
    return await ac.post("/api/auth/login", json={"username": u, "password": p})


async def test_users_admin_only(client):
    assert (await client.get("/api/users")).status_code == 401  # not logged in
    await _login(client)
    assert (await client.get("/api/users")).status_code == 200


async def test_create_update_delete_and_password(client):
    await _login(client)
    r = await client.post("/api/users", json={"username": "bob", "password": "x", "role": "user"})
    assert r.status_code == 201 and "password_hash" not in r.json()
    bob = r.json()

    # duplicate username → 409
    assert (
        await client.post("/api/users", json={"username": "bob", "password": "y"})
    ).status_code == 409

    # change role + reset password, then bob can log in with the new password
    assert (await client.patch(f"/api/users/{bob['id']}", json={"role": "viewer"})).json()[
        "role"
    ] == "viewer"
    await client.patch(f"/api/users/{bob['id']}", json={"password": "newpw"})
    # bob logs in on a fresh client-independent check
    assert (await _login(client, "bob", "newpw")).status_code == 200

    await _login(client)  # back to admin
    assert (await client.delete(f"/api/users/{bob['id']}")).status_code == 200


async def test_cannot_remove_last_admin(client):
    await _login(client)
    users = (await client.get("/api/users")).json()
    root = next(u for u in users if u["username"] == "root")
    # demote → 400 (last admin)
    assert (
        await client.patch(f"/api/users/{root['id']}", json={"role": "user"})
    ).status_code == 400
    # disable → 400
    assert (
        await client.patch(f"/api/users/{root['id']}", json={"enabled": False})
    ).status_code == 400
    # delete → 400
    assert (await client.delete(f"/api/users/{root['id']}")).status_code == 400
