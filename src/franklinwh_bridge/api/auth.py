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

from franklinwh_bridge.gateway.modules import ROLE_CAPABILITIES
from franklinwh_bridge.security import verify_password
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


async def get_current_user(request: Request) -> dict | None:
    """The logged-in user (or None). Returns a synthetic admin under HA ingress."""
    if _is_ingress(request):
        return _ingress_user(request)
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if not user_id:
        return None
    user = await get_user(request.app.state.db, user_id)
    if user is None or not user.get("enabled"):
        return None
    return user


async def require_auth(request: Request) -> dict:
    """Dependency: 401 unless authenticated. Applied to feature routers."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(401, "authentication required")
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
        raise HTTPException(401, "invalid username or password")
    request.session["user_id"] = user["id"]
    await update_user(db, user["id"], last_login_at=time.time())
    return {"user": _public_user(user), "capabilities": capabilities_for(user["role"])}


@router.post("/auth/logout")
async def logout(request: Request):
    if hasattr(request, "session"):
        request.session.clear()
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request) -> dict[str, Any]:
    user = await get_current_user(request)
    if user is None:
        return {"user": None, "capabilities": []}
    return {"user": _public_user(user), "capabilities": capabilities_for(user["role"])}
