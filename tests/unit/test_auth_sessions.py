"""Login record + audit: case-insensitive usernames, why a login ended (the 401
body and /auth/session-status), and the audit trail. Own app, so the conftest
auth bypass does not apply."""

import time
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import require_auth
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.users_api import router as users_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import auth_sessions
from franklinwh_bridge.store.db import create_user, get_user_by_username, init_db


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    db = await init_db(tmp_path / "auth.db")
    root = await create_user(db, "root", hash_password("pw"), role="admin")
    alice = await create_user(db, "alice", hash_password("pw"), role="viewer")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = db
    app.state.config = SimpleNamespace(environment="dev")
    app.include_router(auth_router)
    app.include_router(users_router)

    @app.get("/protected", dependencies=[Depends(require_auth)])
    async def _protected():
        return {"ok": True}

    @app.get("/legacy-login/{user_id}")
    async def _legacy(user_id: str, request: Request):
        # A cookie as written before logins were recorded: user_id, no sid.
        request.session.clear()
        request.session["user_id"] = user_id
        return {"ok": True}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    clients = []

    def new_client():
        c = AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield SimpleNamespace(db=db, root=root, alice=alice, new_client=new_client)
    for c in clients:
        await c.aclose()
    await db.close()


async def _login(c, u, p="pw"):
    return await c.post("/api/auth/login", json={"username": u, "password": p})


async def _events(db, event=None):
    rows = await auth_sessions.list_events(db, limit=100)
    return [r for r in rows if event is None or r["event"] == event]


# ── Case-insensitive usernames ─────────────────────────────────


async def test_login_ignores_username_case(env):
    c = env.new_client()
    r = await _login(c, "ALICE")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "alice"


async def test_create_refuses_a_name_differing_only_in_case(env):
    c = env.new_client()
    await _login(c, "root")
    r = await c.post("/api/users", json={"username": "Alice", "password": "x"})
    assert r.status_code == 409
    assert "alice" in r.json()["detail"]


async def test_exact_match_wins_on_an_older_db_with_case_variants(env):
    # The old case-sensitive UNIQUE allowed both; the exact name must win.
    await create_user(env.db, "Alice", hash_password("other"), role="viewer")
    assert (await get_user_by_username(env.db, "Alice"))["username"] == "Alice"
    assert (await get_user_by_username(env.db, "alice"))["username"] == "alice"


# ── Audit trail ────────────────────────────────────────────────


async def test_login_and_failed_login_are_audited(env):
    c = env.new_client()
    assert (await _login(c, "alice", "wrong")).status_code == 401
    assert (await _login(c, "nobody")).status_code == 401
    r = await _login(c, "alice")
    assert r.json()["session_id"]

    failed = await _events(env.db, "login_failed")
    assert {e["detail"] for e in failed} == {"wrong password", "no such user"}
    assert {e["username"] for e in failed} == {"alice", "nobody"}
    ok = await _events(env.db, "login")
    assert ok[0]["username"] == "alice"
    assert ok[0]["session_id"] == r.json()["session_id"]


async def test_failed_login_for_disabled_account_says_so_in_the_audit(env):
    root = env.new_client()
    await _login(root, "root")
    await root.patch(f"/api/users/{env.alice['id']}", json={"enabled": False})
    c = env.new_client()
    r = await _login(c, "alice")
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid username or password"  # no hint to the caller
    assert (await _events(env.db, "login_failed"))[0]["detail"] == "account disabled"


# ── Why a login ended ──────────────────────────────────────────


async def test_logout_ends_the_login(env):
    c = env.new_client()
    sid = (await _login(c, "alice")).json()["session_id"]
    await c.post("/api/auth/logout")
    st = (await c.get(f"/api/auth/session-status?sid={sid}")).json()
    assert st["state"] == "ended" and st["reason"] == "logout"


async def test_disabled_account_gets_a_401_with_the_reason(env):
    a = env.new_client()
    sid = (await _login(a, "alice")).json()["session_id"]
    assert (await a.get("/protected")).status_code == 200

    root = env.new_client()
    await _login(root, "root")
    await root.patch(f"/api/users/{env.alice['id']}", json={"enabled": False})

    r = await a.get("/protected")
    assert r.status_code == 401
    d = r.json()["detail"]
    assert d["reason"] == "user_disabled"
    assert "disabled" in d["reason_text"]
    assert d["ended_by"] == "root"
    st = (await a.get(f"/api/auth/session-status?sid={sid}")).json()
    assert st["reason"] == "user_disabled"


async def test_admin_revoke_ends_one_login(env):
    a = env.new_client()
    sid = (await _login(a, "alice")).json()["session_id"]
    root = env.new_client()
    await _login(root, "root")

    active = (await root.get("/api/auth/sessions?active=true")).json()
    assert sid in {s["id"] for s in active}
    assert (await root.post(f"/api/auth/sessions/{sid}/revoke")).status_code == 200

    d = (await a.get("/protected")).json()["detail"]
    assert d["reason"] == "revoked" and d["ended_by"] == "root"
    # Revoking twice is a 404, not a second audit row.
    assert (await root.post(f"/api/auth/sessions/{sid}/revoke")).status_code == 404


async def test_admin_password_reset_ends_the_users_logins_but_not_the_admins(env):
    a = env.new_client()
    await _login(a, "alice")
    root = env.new_client()
    await _login(root, "root")

    await root.patch(f"/api/users/{env.alice['id']}", json={"password": "new"})
    assert (await a.get("/protected")).json()["detail"]["reason"] == "password_changed"

    # Changing your own password keeps the login you're using.
    await root.patch(f"/api/users/{env.root['id']}", json={"password": "new2"})
    assert (await root.get("/protected")).status_code == 200


async def test_deleted_account_reason(env):
    a = env.new_client()
    sid = (await _login(a, "alice")).json()["session_id"]
    root = env.new_client()
    await _login(root, "root")
    await root.delete(f"/api/users/{env.alice['id']}")
    st = (await root.get(f"/api/auth/session-status?sid={sid}")).json()
    assert st["reason"] == "user_deleted"
    assert (await a.get("/protected")).status_code == 401


async def test_no_cookie_401_says_no_login(env):
    d = (await env.new_client().get("/protected")).json()["detail"]
    assert d["reason"] == "no_login"
    assert d["reason_text"]


async def test_cookie_dropped_by_browser_is_recorded_when_the_page_asks(env):
    a = env.new_client()
    sid = (await _login(a, "alice")).json()["session_id"]
    a.cookies.clear()  # the browser lost the cookie; the record is still open
    st = (await a.get(f"/api/auth/session-status?sid={sid}")).json()
    assert st["state"] == "ended" and st["reason"] == "browser_dropped"
    assert any(
        e["detail"] == "reason=browser_dropped" for e in await _events(env.db, "session_ended")
    )


async def test_cookie_unused_past_its_lifetime_reads_as_expired(env):
    a = env.new_client()
    sid = (await _login(a, "alice")).json()["session_id"]
    old = time.time() - auth_sessions.COOKIE_MAX_AGE_S - 60
    await env.db.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?", (old, sid))
    await env.db.commit()
    a.cookies.clear()
    st = (await a.get(f"/api/auth/session-status?sid={sid}")).json()
    assert st["reason"] == "expired"


async def test_status_of_the_callers_own_open_login_is_active(env):
    a = env.new_client()
    sid = (await _login(a, "alice")).json()["session_id"]
    st = (await a.get(f"/api/auth/session-status?sid={sid}")).json()
    assert st == {"state": "active", "username": "alice"}


async def test_unknown_sid(env):
    st = (await env.new_client().get("/api/auth/session-status?sid=nope")).json()
    assert st["state"] == "unknown"


# ── Upgrade: logins from before the record existed ─────────────


async def test_cookie_from_before_the_upgrade_is_adopted_not_rejected(env):
    a = env.new_client()
    await a.get(f"/legacy-login/{env.alice['id']}")
    assert (await a.get("/protected")).status_code == 200
    adopted = await _events(env.db, "session_adopted")
    assert adopted and adopted[0]["username"] == "alice"
    # The adopted login is a real one: revoking it works.
    sid = adopted[0]["session_id"]
    root = env.new_client()
    await _login(root, "root")
    await root.post(f"/api/auth/sessions/{sid}/revoke")
    assert (await a.get("/protected")).json()["detail"]["reason"] == "revoked"


async def test_audit_endpoints_are_admin_only(env):
    a = env.new_client()
    await _login(a, "alice")
    assert (await a.get("/api/auth/events")).status_code == 403
    assert (await a.get("/api/auth/sessions")).status_code == 403
    root = env.new_client()
    await _login(root, "root")
    events = (await root.get("/api/auth/events")).json()
    assert {"login"} <= {e["event"] for e in events}
