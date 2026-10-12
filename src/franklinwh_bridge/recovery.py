"""Break-glass admin recovery: get back in when no admin can log in.

Run INSIDE the container, against the database the running bridge uses:

    docker exec -it fwhbridge-app bridge recovery status
    docker exec -it fwhbridge-app bridge recovery reset-admin-password

CLI only, by design: it is for when the web UI and REST API are unusable
because nobody can log in, so there is no session to call them with.

Lessons from FWHAI's recovery tool, which once reported success while editing
a host-side copy of the database and left the operator locked out:
- open only an EXISTING database (never create one at a guessed path);
- print the database path and account count first, so a wrong-database run
  is obvious;
- warn when not running inside a container.

There is deliberately no "turn login off" switch and no master password: on
this bridge an open UI means anyone on the network can command the battery.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from franklinwh_bridge.security import hash_password

MIN_PASSWORD_LEN = 8


class RecoveryError(Exception):
    """A recovery step that must stop with a message, not a traceback."""


def in_container() -> bool:
    return Path("/.dockerenv").exists()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the existing database read-write. Refuses a missing file."""
    if not db_path.is_file():
        raise RecoveryError(
            f"No database at {db_path}. Run this inside the bridge container "
            "(docker exec fwhbridge-app bridge recovery ...), not on the host."
        )
    conn = sqlite3.connect(str(db_path), timeout=15)  # wait out the live bridge's writes
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@dataclass
class Status:
    db_path: Path
    schema: int | None
    users: list[dict]
    in_container: bool

    @property
    def enabled_admins(self) -> list[dict]:
        return [u for u in self.users if u["role"] == "admin" and u["enabled"]]


def status(db_path: Path) -> Status:
    with connect(db_path) as conn:
        schema = None
        if _has_table(conn, "schema_version"):
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            schema = row[0] if row else None
        users = []
        if _has_table(conn, "users"):
            users = [
                {k: r[k] for k in ("username", "role", "enabled", "last_login_at")}
                for r in conn.execute("SELECT * FROM users ORDER BY username")
            ]
    return Status(db_path, schema, users, in_container())


@dataclass
class ResetResult:
    username: str
    password: str
    generated: bool
    action: str  # "reset" | "created"
    changes: list[str]
    sessions_ended: int


def reset_admin_password(
    db_path: Path, username: str = "admin", password: str | None = None
) -> ResetResult:
    """Make ``username`` a working, enabled admin with a known password.

    Covers every lockout: forgotten password (reset), demoted (promoted back
    to admin), disabled (re-enabled), deleted or no accounts at all (created).
    The account's open logins are ended, and the reset is audited.
    """
    username = (username or "").strip()
    if not username:
        raise RecoveryError("username must not be empty")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)
    if len(password) < MIN_PASSWORD_LEN:
        raise RecoveryError(f"password must be at least {MIN_PASSWORD_LEN} characters")

    now = time.time()
    pw_hash = hash_password(password)
    with connect(db_path) as conn:
        if not _has_table(conn, "users"):
            raise RecoveryError(
                f"{db_path} has no users table: not a bridge database, or one that "
                "has never started. Start the bridge once first."
            )
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE "
            "ORDER BY (username = ?) DESC LIMIT 1",
            (username, username),
        ).fetchone()
        changes: list[str] = []
        if row is None:
            user_id = f"user_{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role, enabled, "
                "created_at, updated_at) VALUES (?, ?, ?, 'admin', 1, ?, ?)",
                (user_id, username, pw_hash, now, now),
            )
            action = "created"
            changes.append("created as admin")
        else:
            user_id, username = row["id"], row["username"]
            if row["role"] != "admin":
                changes.append(f"promoted {row['role']} → admin")
            if not row["enabled"]:
                changes.append("re-enabled")
            changes.append("password reset")
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', enabled = 1, "
                "updated_at = ? WHERE id = ?",
                (pw_hash, now, user_id),
            )
            action = "reset"

        ended = 0
        if _has_table(conn, "auth_sessions"):
            ended = conn.execute(
                "UPDATE auth_sessions SET ended_at = ?, end_reason = 'password_changed', "
                "ended_by = 'recovery' WHERE user_id = ? AND ended_at IS NULL",
                (now, user_id),
            ).rowcount
        detail = f"break-glass recovery: {', '.join(changes)}"
        if _has_table(conn, "auth_events"):
            conn.execute(
                "INSERT INTO auth_events (ts, event, username, user_id, detail) "
                "VALUES (?, 'break_glass_reset', ?, ?, ?)",
                (now, username, user_id, detail),
            )
        if _has_table(conn, "logs"):
            # So it shows in the Logs tab, which an admin will read after logging in.
            conn.execute(
                "INSERT INTO logs (ts, level, name, message) VALUES (?, 'WARNING', ?, ?)",
                (now, "franklinwh_bridge.recovery", f"Auth: {detail} (user={username})"),
            )
        conn.commit()
    return ResetResult(username, password, generated, action, changes, ended)
