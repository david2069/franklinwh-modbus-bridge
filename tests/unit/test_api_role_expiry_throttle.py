"""#99: the ``api`` role, account expiry, sign-in audit filters and failed-login
throttling. Own app for the auth flows (the conftest auth bypass does not
apply); the real app for the role gate on /api/command and the UI pages."""

import time
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import require_auth
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.users_api import router as users_router
from franklinwh_bridge.gateway.modules import ROLE_CAPABILITIES
from franklinwh_bridge.login_throttle import LoginThrottle
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import auth_sessions
from franklinwh_bridge.store.db import (
    MIGRATION_COLUMNS,
    MIGRATIONS,
    create_user,
    get_user_by_username,
    get_users,
    init_db,
    run_migrations,
    update_user,
)

# ── Migration 58 ──────────────────────────────────────────────


async def _db_at(path, version: int) -> aiosqlite.Connection:
    """A database migrated only up to ``version``, as an older bridge left it."""
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    for v in sorted(k for k in MIGRATIONS if k <= version):
        for table, column, decl in MIGRATION_COLUMNS.get(v, []):
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                if column not in {r[1] for r in await cur.fetchall()}:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await db.executescript(MIGRATIONS[v])
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (v, time.time())
        )
        await db.commit()
    return db


async def test_migration_58_keeps_every_user_and_accepts_api(tmp_path):
    db = await _db_at(tmp_path / "old.db", 57)
    try:
        await db.execute(
            "INSERT INTO users (id, username, password_hash, role, enabled, created_at, "
            "updated_at, last_login_at) VALUES "
            "('user_a', 'root', 'HASH-A', 'admin', 1, 1, 2, 3), "
            "('user_b', 'bob', 'HASH-B', 'viewer', 0, 4, 5, NULL)"
        )
        await db.commit()
        # Before: the CHECK from migration 32 refuses the new role.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES ('x', 'x', 'h', 'api')"
            )

        assert await run_migrations(db) == max(MIGRATIONS) >= 58

        rows = {u["id"]: u for u in await get_users(db)}
        assert set(rows) == {"user_a", "user_b"}
        assert rows["user_a"]["password_hash"] == "HASH-A"
        assert rows["user_a"]["role"] == "admin" and rows["user_a"]["last_login_at"] == 3
        assert rows["user_b"]["enabled"] is False and rows["user_b"]["created_at"] == 4
        assert rows["user_a"]["expires_at"] is None
        u = await create_user(db, "directconnectapi", "h", role="api", expires_at=123.0)
        assert u["role"] == "api" and u["expires_at"] == 123.0
        # Still refuses a role nobody defined.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES ('y', 'y', 'h', 'root')"
            )
    finally:
        await db.close()


async def test_migration_58_rerun_keeps_expiry(tmp_path):
    """Tests re-apply migrations to an up-to-date table; the rebuild must copy
    expires_at, not drop it."""
    db = await init_db(tmp_path / "a.db")
    try:
        await create_user(db, "z", "h", role="api", expires_at=99.0)
        await db.executescript(MIGRATIONS[58])
        assert (await get_user_by_username(db, "z"))["expires_at"] == 99.0
    finally:
        await db.close()


# ── The grant ─────────────────────────────────────────────────


def test_api_role_is_view_and_control_only():
    """Owner decision 2026-10-15: no admin surface (users, settings, modules)."""
    assert ROLE_CAPABILITIES["api"] == {"view", "control"}


# ── Auth flows on an app of its own ───────────────────────────


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

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    clients = []

    def new_client():
        c = AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield SimpleNamespace(db=db, app=app, root=root, alice=alice, new_client=new_client)
    for c in clients:
        await c.aclose()
    await db.close()


async def _login(c, u, p="pw"):
    return await c.post("/api/auth/login", json={"username": u, "password": p})


async def _events(db, event=None):
    rows = await auth_sessions.list_events(db, limit=500)
    return [r for r in rows if event is None or r["event"] == event]


# ── Expiry ────────────────────────────────────────────────────


async def test_an_expired_account_cannot_sign_in(env):
    await update_user(env.db, env.alice["id"], expires_at=time.time() - 10)
    c = env.new_client()

    r = await _login(c, "alice")

    assert r.status_code == 401
    assert r.json()["detail"] == "invalid username or password"  # same as any failure
    failed = await _events(env.db, "login_failed")
    assert failed[0]["detail"] == "account expired"
    assert failed[0]["user_id"] == env.alice["id"]


async def test_a_future_expiry_still_signs_in(env):
    await update_user(env.db, env.alice["id"], expires_at=time.time() + 3600)

    assert (await _login(env.new_client(), "alice")).status_code == 200


async def test_an_open_login_ends_once_the_account_expires(env):
    c = env.new_client()
    r = await _login(c, "alice")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert (await c.get("/protected")).status_code == 200

    await update_user(env.db, env.alice["id"], expires_at=time.time() - 1)
    r = await c.get("/protected")

    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "expired"
    s = await auth_sessions.get_session(env.db, sid)
    assert s["end_reason"] == "expired" and s["ended_by"] == "account expiry date"
    ended = await _events(env.db, "session_ended")
    assert any(e["session_id"] == sid and "reason=expired" in e["detail"] for e in ended)
    # And the cookie is dropped: the next request has no login at all.
    assert (await c.get("/protected")).status_code == 401


async def test_users_api_sets_and_clears_expiry(env):
    c = env.new_client()
    await _login(c, "root")
    exp = time.time() + 86400 * 30
    r = await c.post(
        "/api/users",
        json={
            "username": "directconnectapi",
            "password": "x" * 40,
            "role": "api",
            "expires_at": exp,
        },
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert r.json()["role"] == "api" and r.json()["expires_at"] == exp

    # Leaving expires_at out keeps it; null clears it.
    r = await c.patch(f"/api/users/{uid}", json={"enabled": True})
    assert r.json()["expires_at"] == exp
    r = await c.patch(f"/api/users/{uid}", json={"expires_at": None})
    assert r.json()["expires_at"] is None


async def test_expiry_in_the_past_ends_open_logins_now(env):
    alice = env.new_client()
    sid = (await _login(alice, "alice")).json()["session_id"]
    admin = env.new_client()
    await _login(admin, "root")

    r = await admin.patch(f"/api/users/{env.alice['id']}", json={"expires_at": time.time() - 5})

    assert r.status_code == 200
    assert (await auth_sessions.get_session(env.db, sid))["end_reason"] == "expired"


async def test_last_admin_cannot_be_given_an_expiry(env):
    c = env.new_client()
    await _login(c, "root")

    r = await c.patch(f"/api/users/{env.root['id']}", json={"expires_at": time.time() + 999})

    assert r.status_code == 400


async def test_unknown_role_still_refused(env):
    c = env.new_client()
    await _login(c, "root")

    r = await c.post("/api/users", json={"username": "q", "password": "p", "role": "root"})

    assert r.status_code == 422


# ── Audit filters ─────────────────────────────────────────────


async def test_events_filter_by_user_and_event(env):
    await _login(env.new_client(), "alice", "nope")
    await _login(env.new_client(), "ALICE")
    c = env.new_client()
    await _login(c, "root")

    by_user = (await c.get("/api/auth/events", params={"username": "alice"})).json()
    assert by_user and all(e["username"].lower() == "alice" for e in by_user)
    failed = (await c.get("/api/auth/events", params={"event": "login_failed"})).json()
    assert failed and all(e["event"] == "login_failed" for e in failed)
    both = (
        await c.get("/api/auth/events", params={"username": "root", "event": "login_failed"})
    ).json()
    assert both == []
    ts = [e["ts"] for e in (await c.get("/api/auth/events")).json()]
    assert ts == sorted(ts, reverse=True)


async def test_an_api_account_cannot_read_the_audit_or_users(env):
    """Admin-only routes that are gated today (#102 covers the ungated ones)."""
    await create_user(env.db, "svc", hash_password("pw"), role="api")
    c = env.new_client()
    assert (await _login(c, "svc")).status_code == 200

    assert (await c.get("/api/users")).status_code == 403
    assert (await c.get("/api/auth/events")).status_code == 403
    assert (
        await c.post("/api/users", json={"username": "x", "password": "y", "role": "admin"})
    ).status_code == 403


# ── Throttling ────────────────────────────────────────────────


async def test_five_failures_lock_the_pair_out(env):
    c = env.new_client()
    for _ in range(5):
        assert (await _login(c, "alice", "wrong")).status_code == 401

    r = await _login(c, "alice", "wrong")
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # The right password is refused too: a lockout is not a password oracle.
    assert (await _login(c, "alice", "pw")).status_code == 429
    await _login(c, "alice", "wrong")

    throttled = await _events(env.db, "login_throttled")
    assert len(throttled) == 1  # once per lockout, not per attempt
    assert throttled[0]["username"] == "alice"
    # Attempts refused during the lockout are not counted as login_failed.
    assert len(await _events(env.db, "login_failed")) == 5


async def test_unknown_names_are_throttled_the_same(env):
    """The 429 must not say whether the name exists."""
    c = env.new_client()
    for _ in range(5):
        assert (await _login(c, "nobody", "x")).status_code == 401

    r = await _login(c, "nobody", "x")

    assert r.status_code == 429 and "retry-after" in r.headers
    assert r.json()["detail"] == (await _login(c, "nobody", "x")).json()["detail"]


async def test_lockout_is_per_username(env):
    c = env.new_client()
    for _ in range(5):
        await _login(c, "alice", "wrong")

    assert (await _login(c, "root")).status_code == 200


async def test_a_successful_sign_in_resets_the_count(env):
    c = env.new_client()
    for _ in range(4):
        await _login(c, "alice", "wrong")
    assert (await _login(c, "alice")).status_code == 200
    for _ in range(4):
        assert (await _login(c, "alice", "wrong")).status_code == 401

    assert (await _login(c, "alice")).status_code == 200
    assert await _events(env.db, "login_throttled") == []


def test_throttle_window_and_lockout_timing():
    t = LoginThrottle(max_failures=5, window_s=900, lockout_s=60)
    key = ("alice", "1.2.3.4")
    for i in range(4):
        assert t.record_failure(key, now=100 + i) is False
    assert t.retry_after(key, now=104) is None
    assert t.record_failure(key, now=104) is True  # the 5th starts the lockout
    assert t.retry_after(key, now=105) == 59
    assert t.retry_after(key, now=164) is None  # 60 s later
    # Still inside the 15-min window: the next failure locks again at once.
    assert t.record_failure(key, now=170) is True
    # Failures older than the window no longer count.
    t2 = LoginThrottle(max_failures=5, window_s=900, lockout_s=60)
    for i in range(4):
        t2.record_failure(key, now=i)
    assert t2.record_failure(key, now=1000) is False


# ── The real app: the control gate and the UI pages ───────────


@pytest.fixture
def _real_auth():
    from franklinwh_bridge.api.auth import get_current_user
    from franklinwh_bridge.main import app

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)
    yield


@pytest.fixture
async def real_client(_real_auth, tmp_path, monkeypatch):
    from franklinwh_bridge.main import app

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        await create_user(app.state.db, "svc", hash_password("pw12345678"), role="api")
        r = await ac.post("/api/auth/login", json={"username": "svc", "password": "pw12345678"})
        assert r.status_code == 200, r.text
        yield ac


async def test_an_api_account_reaches_the_command_route(real_client):
    resp = await real_client.post("/api/command", json={"slug": "battery_command", "value": "Stop"})

    assert resp.status_code != 403


async def test_an_api_account_is_refused_admin_routes(real_client):
    assert (await real_client.get("/api/users")).status_code == 403
    assert (await real_client.get("/api/auth/events")).status_code == 403


@pytest.mark.parametrize("path", ["/", "/user", "/login"])
async def test_an_api_account_gets_no_web_pages(real_client, path):
    r = await real_client.get(path, follow_redirects=False)

    assert r.status_code == 403
    assert "REST API only" in r.text
