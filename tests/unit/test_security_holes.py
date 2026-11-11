"""Backups and logs are admin-only (#459); the generated admin password stays out of
the logs table and the add-on log (#460, #185); X-Forwarded-Proto is believed only
from a trusted proxy (#201)."""

from __future__ import annotations

import collections
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.admin import router as admin_router
from franklinwh_bridge.api.auth import require_admin_for_writes, require_auth
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.main import LogBufferHandler
from franklinwh_bridge.security import hash_password, seed_admin
from franklinwh_bridge.store.db import create_user, init_db

ROLES = ("viewer", "user", "api")
ADMIN_GETS = ("/api/logs", "/api/backup/list", "/api/backup/download/any")


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "sec.db")
    yield conn
    await conn.close()


def _app(db) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.state.db = db
    app.state.config = SimpleNamespace(environment="dev")
    app.state.backup_manager = SimpleNamespace(_backup_dir=Path("/nonexistent"),
                                               list_backups=lambda: [])
    app.include_router(auth_router)
    app.include_router(admin_router, dependencies=[Depends(require_auth),
                                                   Depends(require_admin_for_writes)])
    return app


async def _client(db, peer=("192.0.2.20", 5000)) -> AsyncClient:
    transport = ASGITransport(app=_app(db), raise_app_exceptions=False, client=peer)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_non_admin_cannot_read_logs_or_backups(db, monkeypatch, role, path):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    await create_user(db, "u", hash_password("pw"), role=role)
    async with await _client(db) as c:
        r = await c.post("/api/auth/login", json={"username": "u", "password": "pw"})
        assert r.status_code == 200
        assert (await c.get(path)).status_code == 403


@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_admin_still_reads_logs_and_backups(db, monkeypatch, path):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    await create_user(db, "a", hash_password("pw"), role="admin")
    async with await _client(db) as c:
        await c.post("/api/auth/login", json={"username": "a", "password": "pw"})
        assert (await c.get(path)).status_code != 403


def _capture():
    """The Logs tab handler (buffer + persist queue) and a console stand-in."""
    buf: collections.deque = collections.deque()
    queue: collections.deque = collections.deque()
    tab = LogBufferHandler(buf, queue)
    tab.setFormatter(logging.Formatter("%(message)s"))
    console: list[str] = []
    con = logging.Handler()
    con.emit = lambda r: console.append(r.getMessage())  # type: ignore[method-assign]
    log = logging.getLogger("franklinwh_bridge")
    log.addHandler(tab)
    log.addHandler(con)
    return log, tab, con, buf, queue, console


async def _seed(db, monkeypatch, env):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("APP_ENV", env)
    pw = "Generated-Secret-123"
    monkeypatch.setattr("franklinwh_bridge.security.generate_password", lambda: pw)
    log, tab, con, buf, queue, console = _capture()
    try:
        await seed_admin(db)
    finally:
        log.removeHandler(tab)
        log.removeHandler(con)
    return pw, buf, queue, console


async def test_generated_password_never_reaches_the_logs_tab(db, monkeypatch):
    pw, buf, queue, console = await _seed(db, monkeypatch, "docker")
    assert not any(pw in e["message"] for e in [*buf, *queue])
    assert any(pw in line for line in console)  # Docker/Python still see it once


async def test_addon_never_prints_the_generated_password(db, monkeypatch):
    pw, buf, queue, console = await _seed(db, monkeypatch, "ha_addon")
    lines = [e["message"] for e in [*buf, *queue]] + console
    assert not any(pw in line for line in lines)
    assert not any("ADMIN_PASSWORD" in line for line in lines)
    assert any("Settings → Admin → Users" in line for line in lines)


async def test_scrub_removes_passwords_older_versions_stored(db):
    from franklinwh_bridge.security import scrub_logged_passwords  # new in this fix

    await db.execute(
        "INSERT INTO logs (ts, level, name, message) VALUES (1, 'WARNING', 's', "
        "'Seeded admin user ''admin'' with a GENERATED password: abc  (set ADMIN_PASSWORD …)')")
    await db.execute("INSERT INTO logs (ts, level, name, message) VALUES (2, 'INFO', 's', 'other')")
    await db.commit()
    assert await scrub_logged_passwords(db) == 1
    async with db.execute("SELECT message FROM logs") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["other"]


@pytest.mark.parametrize(("peer", "trusted", "status"), [
    ("192.0.2.20", None, 400),                 # any client: header ignored
    ("127.0.0.1", None, 200),                  # local proxy (Caddy, tailscale serve)
    ("192.0.2.30", "192.0.2.0/24", 200),       # configured proxy network
    ("198.51.100.7", "192.0.2.30", 400),       # not the configured proxy
])
async def test_forwarded_proto_only_from_trusted_proxy(db, monkeypatch, peer, trusted, status):
    monkeypatch.delenv("ALLOW_INSECURE_AUTH", raising=False)
    if trusted:
        monkeypatch.setenv("TRUSTED_PROXIES", trusted)
    else:
        monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    await create_user(db, "a", hash_password("pw"), role="admin")
    async with await _client(db, peer=(peer, 5000)) as c:
        r = await c.post("/api/auth/login", json={"username": "a", "password": "pw"},
                         headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == status
