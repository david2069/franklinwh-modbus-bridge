"""Authentication — login/logout/me + the ``require_auth`` / ``require_role``
dependencies. Sessions ride Starlette's signed cookies (SessionMiddleware); no
bespoke crypto. Under HA ingress the Supervisor already authenticated the user,
so auth is bypassed there. See docs/multi-user-and-pwa-design.md §3.
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.gateway.modules import ROLE_CAPABILITIES
from franklinwh_bridge.security import hash_password, verify_password
from franklinwh_bridge.store import auth_sessions
from franklinwh_bridge.store.db import (
    get_user,
    get_user_by_username,
    update_user,
)

router = APIRouter(prefix="/api", tags=["auth"])

# Synthetic admin used under HA ingress (Supervisor authenticated the user).
# The id stays "ingress" for every HA user (it keys the legal-notice
# acknowledgement and the audit trail); the NAME is the HA user's own.
_INGRESS_ADMIN = {"id": "ingress", "username": "Home Assistant", "role": "admin", "enabled": True}


def _ingress_user(request: Request) -> dict:
    """The ingress admin, named after the Home Assistant user.

    The Supervisor's ingress proxy passes the signed-in HA user as
    X-Remote-User-Display-Name / -Name. Without it the UI showed the
    placeholder "(ingress)", and its first letter "(" as the avatar.
    """
    name = (
        request.headers.get("x-remote-user-display-name")
        or request.headers.get("x-remote-user-name")
        or ""
    ).strip()
    return {**_INGRESS_ADMIN, "username": name[:80]} if name else _INGRESS_ADMIN


def _is_ingress(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    return bool(config and str(getattr(config, "environment", "")) == "ha_addon")


def _is_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def _public_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "password_hash"}


def capabilities_for(role: str) -> list[str]:
    return sorted(ROLE_CAPABILITIES.get(role, set()))


def _end_reason(request: Request, code: str, session: dict | None = None) -> None:
    """Remember why this request has no user, for require_auth's 401 body."""
    request.state.auth_end = {
        "reason": code,
        "reason_text": auth_sessions.reason_text(code),
        "ended_at": (session or {}).get("ended_at"),
        "ended_by": (session or {}).get("ended_by"),
    }


async def get_current_user(request: Request) -> dict | None:
    """The logged-in user (or None). Returns a synthetic admin under HA ingress.

    The cookie names the user and the login (``sid``); the login's row in
    ``auth_sessions`` must still be open. A cookie from before logins were
    recorded has no ``sid`` and is adopted (given a row) rather than rejected,
    so upgrading doesn't log everyone out.
    """
    if _is_ingress(request):
        return _ingress_user(request)
    if not hasattr(request, "session"):
        return None
    user_id = request.session.get("user_id")
    if not user_id:
        _end_reason(request, "no_login")
        return None
    db = request.app.state.db
    sid = request.session.get("sid")
    session = await auth_sessions.get_session(db, sid) if sid else None
    if sid and (session is None or session.get("ended_at")):
        # Ended (logout elsewhere, disabled, revoked) or unknown: drop the
        # cookie so the browser stops presenting a dead login.
        _end_reason(request, (session or {}).get("end_reason") or "unknown", session)
        request.session.clear()
        return None

    user = await get_user(db, user_id)
    if user is None or not user.get("enabled"):
        code = "user_deleted" if user is None else "user_disabled"
        if sid:
            await auth_sessions.end_session(db, sid, code, request=request)
            session = await auth_sessions.get_session(db, sid)
        _end_reason(request, code, session)
        request.session.clear()
        return None

    if session is None:
        sid = await auth_sessions.create_session(db, user, request)
        request.session["sid"] = sid
        await auth_sessions.record_event(
            db, "session_adopted", username=user["username"], user_id=user["id"],
            session_id=sid, request=request,
            detail="login from before the login record existed",
        )
    else:
        await auth_sessions.touch(db, session)
    request.state.auth_sid = sid
    request.state.auth_user = user
    return user


async def require_auth(request: Request) -> dict:
    """Dependency: 401 unless authenticated. Applied to feature routers.

    The 401 body says why (``reason`` code + ``reason_text``), so the page can
    tell the user instead of showing "Disconnected".
    """
    user = await get_current_user(request)
    if user is None:
        end = getattr(request.state, "auth_end", None) or {}
        raise HTTPException(401, {"message": "authentication required", **end})
    return user


def require_role(*roles: str):
    """Dependency factory: require one of ``roles`` (implies require_auth)."""

    async def _dep(user: dict = Depends(require_auth)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(403, "insufficient role")
        return user

    return _dep


def require_capability(capability: str):
    """Dependency factory: require a capability from ``ROLE_CAPABILITIES``.

    Prefer this over :func:`require_role` for anything that acts on hardware.
    ROLE_CAPABILITIES has always reserved ``control`` to roles that may operate
    the battery, but nothing enforced it at the routes — every control endpoint
    asked only for a *session*, so any signed-in viewer could dispatch a forced
    charge over the API. Naming the capability keeps the grant and the check in
    one place instead of restating the role list at each call site.
    """

    async def _dep(user: dict = Depends(require_auth)) -> dict:
        role = user.get("role", "")
        if capability not in ROLE_CAPABILITIES.get(role, set()):
            raise HTTPException(403, f"'{capability}' capability required")
        return user

    return _dep


async def _read_credentials(request: Request) -> tuple[str, str]:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        data = await request.json()
        return str(data.get("username", "")), str(data.get("password", ""))
    form = await request.form()
    return str(form.get("username", "")), str(form.get("password", ""))


@router.post("/auth/login")
async def login(request: Request):
    """Verify credentials, set the session cookie. Accepts form or JSON."""
    if not _is_ingress(request) and not _is_secure(request) and os.environ.get(
        "ALLOW_INSECURE_AUTH"
    ) != "1":
        raise HTTPException(
            400, "login requires HTTPS — set ALLOW_INSECURE_AUTH=1 for a LAN-only setup"
        )
    username, password = await _read_credentials(request)
    db = request.app.state.db
    user = await get_user_by_username(db, username)
    if user is None or not user.get("enabled") or not verify_password(
        user["password_hash"], password
    ):
        why = (
            "no such user" if user is None
            else "account disabled" if not user.get("enabled")
            else "wrong password"
        )
        await auth_sessions.record_event(
            db, "login_failed", username=username[:64],
            user_id=(user or {}).get("id"), request=request, detail=why,
        )
        raise HTTPException(401, "invalid username or password")
    request.session.clear()
    sid = await auth_sessions.create_session(db, user, request)
    request.session["user_id"] = user["id"]
    request.session["sid"] = sid
    await update_user(db, user["id"], last_login_at=time.time())
    await auth_sessions.record_event(
        db, "login", username=user["username"], user_id=user["id"],
        session_id=sid, request=request,
    )
    return {
        "user": _public_user(user),
        "capabilities": capabilities_for(user["role"]),
        # The page keeps this so it can ask why, if the cookie later vanishes.
        "session_id": sid,
    }


@router.post("/auth/logout")
async def logout(request: Request):
    if hasattr(request, "session"):
        sid = request.session.get("sid")
        if sid:
            await auth_sessions.end_session(
                request.app.state.db, sid, "logout", request=request
            )
        request.session.clear()
    return {"ok": True}


@router.get("/auth/session-status")
async def session_status(request: Request, sid: str = "") -> dict[str, Any]:
    """Why a login ended. No auth: it's asked by a page that just got a 401.

    ``sid`` is the login id the page saved at login time; it is a random
    secret only that browser holds. If the record says the login is still
    open but the browser has stopped presenting it, it is ended here with the
    reason that fits (idle past the cookie lifetime = expired, else the
    browser dropped it), so the audit trail has it too.
    """
    db = request.app.state.db
    if not sid:
        return {"state": "unknown", "reason": "no_login",
                "reason_text": auth_sessions.reason_text("no_login")}
    s = await auth_sessions.get_session(db, sid)
    if s is None:
        return {"state": "unknown", "reason": "unknown",
                "reason_text": auth_sessions.reason_text("unknown")}
    if not s.get("ended_at"):
        current = request.session.get("sid") if hasattr(request, "session") else None
        if current == sid:
            return {"state": "active", "username": s["username"]}
        idle = time.time() - (s.get("last_seen_at") or 0)
        code = "expired" if idle >= auth_sessions.COOKIE_MAX_AGE_S else "browser_dropped"
        await auth_sessions.end_session(db, sid, code, request=request)
        s = await auth_sessions.get_session(db, sid) or s
    return {
        "state": "ended",
        "username": s["username"],
        "reason": s.get("end_reason"),
        "reason_text": auth_sessions.reason_text(s.get("end_reason")),
        "ended_at": s.get("ended_at"),
        "ended_by": s.get("ended_by"),
        "last_seen_at": s.get("last_seen_at"),
    }


#: Passwords anyone would try first. A password in this set (or equal to the
#: username) is reported as weak by the setup wizard and refused here.
GUESSABLE_PASSWORDS = frozenset({"admin", "password", "franklin", "franklinwh", "12345678"})
MIN_PASSWORD_LEN = 8


def is_guessable(user: dict) -> bool:
    """True if the account's password is a default anyone would try first."""
    candidates = GUESSABLE_PASSWORDS | {user.get("username", ""), user.get("username", "").lower()}
    return any(c and verify_password(user["password_hash"], c) for c in candidates)


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=MIN_PASSWORD_LEN, max_length=200)


@router.post("/auth/password")
async def change_own_password(body: PasswordChange, request: Request):
    """Change your own password. Your other logins end; this one stays."""
    if _is_ingress(request):
        raise HTTPException(400, "signed in through Home Assistant: no bridge password to change")
    user = await require_auth(request)
    db = request.app.state.db
    fresh = await get_user(db, user["id"])
    if fresh is None or not verify_password(fresh["password_hash"], body.current_password):
        await auth_sessions.record_event(
            db, "password_change_failed", username=user["username"], user_id=user["id"],
            request=request, detail="wrong current password",
        )
        raise HTTPException(400, "current password is wrong")
    new = body.new_password
    if new.lower() in GUESSABLE_PASSWORDS or new.lower() == user["username"].lower():
        raise HTTPException(400, "choose a password that isn't a common default or your username")
    if verify_password(fresh["password_hash"], new):
        raise HTTPException(400, "the new password is the same as the current one")
    await update_user(db, user["id"], password_hash=hash_password(new))
    own_sid = getattr(request.state, "auth_sid", None)
    ended = await auth_sessions.end_user_sessions(
        db, user["id"], "password_changed", by=user["username"],
        except_sid=own_sid, request=request,
    )
    await auth_sessions.record_event(
        db, "password_changed", username=user["username"], user_id=user["id"],
        session_id=own_sid, request=request, detail=f"self-service; {ended} other login(s) ended",
    )
    return {"ok": True, "other_logins_ended": ended}


@router.get("/auth/me")
async def me(request: Request) -> dict[str, Any]:
    user = await get_current_user(request)
    if user is None:
        return {"user": None, "capabilities": []}
    return {"user": _public_user(user), "capabilities": capabilities_for(user["role"])}


# ── Admin: who is logged in, and the audit trail ──────────────

# Hoisted: Depends() in an argument default trips ruff B008.
_REQUIRE_ADMIN = Depends(require_role("admin"))


@router.get("/auth/sessions", dependencies=[Depends(require_role("admin"))])
async def list_sessions(request: Request, active: bool = False, limit: int = 200):
    """Logins, most recently active first. ``active=true`` = still open."""
    return await auth_sessions.list_sessions(
        request.app.state.db, active_only=active, limit=max(1, min(limit, 1000))
    )


@router.post("/auth/sessions/{sid}/revoke")
async def revoke_session(
    sid: str, request: Request, user: dict = _REQUIRE_ADMIN
):
    """End one login now; its page shows "An administrator ended this login"."""
    ok = await auth_sessions.end_session(
        request.app.state.db, sid, "revoked", by=user["username"], request=request
    )
    if not ok:
        raise HTTPException(404, "no open login with that id")
    return {"revoked": sid}


@router.get("/auth/events", dependencies=[Depends(require_role("admin"))])
async def list_auth_events(request: Request, limit: int = 200):
    """Audit trail: logins, failed logins, logouts, logins ended (newest first)."""
    return await auth_sessions.list_events(
        request.app.state.db, limit=max(1, min(limit, 1000))
    )
