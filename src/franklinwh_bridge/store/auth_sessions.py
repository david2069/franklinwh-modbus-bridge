"""Login record and audit trail (``auth_sessions`` + ``auth_events``).

A login used to be only a signed cookie. When a page lost it, every request got
a bare 401: the page showed "Disconnected", and nobody could say why. Each login
now has a row, and how it ended is recorded, so the page can show the reason and
an admin can see who logged in, from where, and what failed.

The cookie still carries the identity. The row is the record, and the switch an
admin or a disabled account uses to end a login before the cookie expires.
"""

from __future__ import annotations

import logging
import secrets
import time

import aiosqlite

logger = logging.getLogger("franklinwh_bridge.auth")

#: Starlette's SessionMiddleware default cookie lifetime. The cookie is re-sent
#: on every response, so this is time since the browser last used the bridge.
COOKIE_MAX_AGE_S = 14 * 24 * 3600

#: last_seen_at is written at most this often, not on every 10 s poll.
_TOUCH_EVERY_S = 60.0

#: Why a login ended, in words a person can act on. Codes are stored; text is
#: what the logged-out modal and the login page show.
REASONS: dict[str, str] = {
    "logout": "You logged out.",
    "user_disabled": "Your account was disabled by an administrator.",
    "user_deleted": "Your account was deleted by an administrator.",
    "password_changed": (
        "Your password was changed — by you on another device, by an "
        "administrator, or by admin recovery."
    ),
    "revoked": "An administrator ended this login.",
    # Also used when the ACCOUNT's expiry date passes (#99); ended_by then
    # says "account expiry date", so the page shows which one it was.
    "expired": (
        "Your login expired: 14 days without use, or your account's expiry "
        "date has passed."
    ),
    "browser_dropped": (
        "This browser stopped sending its login: site data was cleared, "
        "private browsing ended, or the browser removed the cookie."
    ),
    "no_login": (
        "This browser has no saved login for this address. It may have been "
        "cleared, or the bridge was opened by a different address (IP, "
        "hostname or VPN)."
    ),
    "unknown": "The bridge has no record of this login.",
}


def reason_text(code: str | None) -> str:
    return REASONS.get(code or "unknown", REASONS["unknown"])


def _client(request) -> tuple[str | None, str | None]:
    """IP and user agent, for the audit row. Request may be None in tests."""
    if request is None:
        return None, None
    ip = request.client.host if getattr(request, "client", None) else None
    ua = (request.headers.get("user-agent") or "")[:300] or None
    return ip, ua


async def record_event(
    db: aiosqlite.Connection,
    event: str,
    *,
    username: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    request=None,
    detail: str | None = None,
) -> None:
    """Append an audit row and log it, so it also shows in the Logs tab."""
    ip, ua = _client(request)
    await db.execute(
        "INSERT INTO auth_events (ts, event, username, user_id, session_id, ip, "
        "user_agent, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), event, username, user_id, session_id, ip, ua, detail),
    )
    await db.commit()
    level = logging.WARNING if event in ("login_failed", "login_throttled") else logging.INFO
    logger.log(
        level, "Auth: %s user=%s ip=%s%s",
        event, username or "-", ip or "-", f" ({detail})" if detail else "",
    )


async def create_session(db: aiosqlite.Connection, user: dict, request=None) -> str:
    sid = secrets.token_urlsafe(24)
    ip, ua = _client(request)
    now = time.time()
    await db.execute(
        "INSERT INTO auth_sessions (id, user_id, username, created_at, last_seen_at, "
        "ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, user["id"], user["username"], now, now, ip, ua),
    )
    await db.commit()
    return sid


async def get_session(db: aiosqlite.Connection, sid: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM auth_sessions WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def touch(db: aiosqlite.Connection, session: dict) -> None:
    now = time.time()
    if now - (session.get("last_seen_at") or 0) < _TOUCH_EVERY_S:
        return
    await db.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?", (now, session["id"]))
    await db.commit()


async def end_session(
    db: aiosqlite.Connection,
    sid: str,
    reason: str,
    *,
    by: str | None = None,
    request=None,
) -> bool:
    """End one login and audit it. False if it was already ended or unknown."""
    s = await get_session(db, sid)
    if s is None or s.get("ended_at"):
        return False
    await db.execute(
        "UPDATE auth_sessions SET ended_at = ?, end_reason = ?, ended_by = ? WHERE id = ?",
        (time.time(), reason, by, sid),
    )
    await db.commit()
    await record_event(
        db, "session_ended", username=s["username"], user_id=s["user_id"],
        session_id=sid, request=request,
        detail=f"reason={reason}" + (f" by={by}" if by else ""),
    )
    return True


async def end_user_sessions(
    db: aiosqlite.Connection,
    user_id: str,
    reason: str,
    *,
    by: str | None = None,
    except_sid: str | None = None,
    request=None,
) -> int:
    """End every open login of one user (account disabled, deleted, password reset)."""
    async with db.execute(
        "SELECT id FROM auth_sessions WHERE user_id = ? AND ended_at IS NULL", (user_id,)
    ) as cur:
        sids = [r[0] for r in await cur.fetchall()]
    n = 0
    for sid in sids:
        if sid != except_sid and await end_session(db, sid, reason, by=by, request=request):
            n += 1
    return n


async def list_sessions(
    db: aiosqlite.Connection, active_only: bool = False, limit: int = 200
) -> list[dict]:
    db.row_factory = aiosqlite.Row
    where = "WHERE ended_at IS NULL" if active_only else ""
    async with db.execute(
        f"SELECT * FROM auth_sessions {where} ORDER BY last_seen_at DESC LIMIT ?",  # noqa: S608
        (limit,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def list_events(
    db: aiosqlite.Connection,
    limit: int = 200,
    *,
    username: str | None = None,
    event: str | None = None,
) -> list[dict]:
    """Newest first. ``username`` matches ignoring case; ``event`` exactly."""
    db.row_factory = aiosqlite.Row
    where, args = [], []
    if username:
        where.append("username = ? COLLATE NOCASE")
        args.append(username)
    if event:
        where.append("event = ?")
        args.append(event)
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    async with db.execute(
        f"SELECT * FROM auth_events {clause}ORDER BY ts DESC, id DESC LIMIT ?",  # noqa: S608
        (*args, limit),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]
