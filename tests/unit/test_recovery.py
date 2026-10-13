"""Break-glass admin recovery: every lockout is recoverable, the wrong database
is refused rather than created, and the reset is audited."""

import sqlite3

import pytest
from typer.testing import CliRunner

from franklinwh_bridge import cli
from franklinwh_bridge.recovery import RecoveryError, reset_admin_password, status
from franklinwh_bridge.security import hash_password, verify_password
from franklinwh_bridge.store.db import create_user, init_db, update_user


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "bridge.db"
    db = await init_db(path)
    await create_user(db, "admin", hash_password("old-password"), role="admin")
    await create_user(db, "viewer1", hash_password("pw"), role="viewer")
    await db.close()
    return path


def _user(path, name):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone())


def test_forgotten_password_is_reset(db_path):
    r = reset_admin_password(db_path, password="new-password-1")
    assert r.action == "reset" and not r.generated
    assert verify_password(_user(db_path, "admin")["password_hash"], "new-password-1")
    assert not verify_password(_user(db_path, "admin")["password_hash"], "old-password")


async def test_demoted_and_disabled_admin_is_restored(db_path):
    db = await init_db(db_path)
    uid = _user(db_path, "admin")["id"]
    await update_user(db, uid, role="viewer", enabled=False)
    await db.close()

    r = reset_admin_password(db_path, password="new-password-1")
    u = _user(db_path, "admin")
    assert u["role"] == "admin" and u["enabled"] == 1
    assert "promoted viewer → admin" in r.changes and "re-enabled" in r.changes


def test_deleted_admin_is_created(db_path):
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM users WHERE username = 'admin'")
    r = reset_admin_password(db_path, password="new-password-1")
    assert r.action == "created"
    assert _user(db_path, "admin")["role"] == "admin"


def test_no_accounts_at_all(db_path):
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM users")
    assert not status(db_path).enabled_admins
    reset_admin_password(db_path, username="rescue", password="new-password-1")
    assert [u["username"] for u in status(db_path).enabled_admins] == ["rescue"]


def test_username_matches_ignoring_case(db_path):
    r = reset_admin_password(db_path, username="ADMIN", password="new-password-1")
    assert r.action == "reset" and r.username == "admin"


def test_generated_password_is_strong_and_works(db_path):
    r = reset_admin_password(db_path)
    assert r.generated and len(r.password) >= 16
    assert verify_password(_user(db_path, "admin")["password_hash"], r.password)


def test_short_password_refused(db_path):
    with pytest.raises(RecoveryError, match="at least 8"):
        reset_admin_password(db_path, password="short")


def test_missing_database_is_refused_not_created(tmp_path):
    missing = tmp_path / "nope" / "bridge.db"
    with pytest.raises(RecoveryError, match="inside the bridge container"):
        status(missing)
    with pytest.raises(RecoveryError):
        reset_admin_password(missing, password="new-password-1")
    assert not missing.exists()


def test_reset_is_audited_in_the_logs_tab(db_path):
    reset_admin_password(db_path, password="new-password-1")
    with sqlite3.connect(db_path) as c:
        msgs = [r[0] for r in c.execute("SELECT message FROM logs WHERE level = 'WARNING'")]
    assert any("break-glass recovery" in m and "user=admin" in m for m in msgs)


def test_ends_open_logins_and_writes_auth_event(db_path):
    uid = _user(db_path, "admin")["id"]
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO auth_sessions (id, user_id, username, created_at, last_seen_at)"
            " VALUES ('s1', ?, 'admin', 1, 1)", (uid,),
        )
    r = reset_admin_password(db_path, password="new-password-1")
    assert r.sessions_ended == 1
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT end_reason, ended_by FROM auth_sessions").fetchone() == (
            "password_changed", "recovery",
        )
        assert c.execute("SELECT event FROM auth_events").fetchall() == [("break_glass_reset",)]


def test_cli_reset_refuses_outside_a_container_unless_allowed(db_path, monkeypatch):
    monkeypatch.setattr(cli, "_recovery_db", lambda: db_path)
    monkeypatch.setattr("franklinwh_bridge.recovery.in_container", lambda: False)
    before = _user(db_path, "admin")["password_hash"]
    out = CliRunner().invoke(cli.app, ["recovery", "reset-admin-password", "--yes"])
    assert out.exit_code == 2 and "Refusing" in out.output
    assert _user(db_path, "admin")["password_hash"] == before
    out = CliRunner().invoke(
        cli.app, ["recovery", "reset-admin-password", "--yes", "--allow-host"]
    )
    assert out.exit_code == 0, out.output


def test_cli_status_and_reset(db_path, monkeypatch):
    monkeypatch.setattr(cli, "_recovery_db", lambda: db_path)
    monkeypatch.setattr("franklinwh_bridge.recovery.in_container", lambda: True)
    run = CliRunner()

    out = run.invoke(cli.app, ["recovery", "status"])
    assert out.exit_code == 0, out.output
    assert str(db_path) in out.output and "admin" in out.output and "viewer1" in out.output

    out = run.invoke(cli.app, ["recovery", "reset-admin-password", "--yes"])
    assert out.exit_code == 0, out.output
    assert "password reset" in out.output and "shown once" in out.output

    # Declining the prompt changes nothing.
    before = _user(db_path, "admin")["password_hash"]
    out = run.invoke(cli.app, ["recovery", "reset-admin-password"], input="n\n")
    assert out.exit_code == 1
    assert _user(db_path, "admin")["password_hash"] == before
