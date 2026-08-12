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
_INGRESS_ADMIN = {"id": "ingress", "username": "(ingress)", "role": "admin", "enabled": True}


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
        return _INGRESS_ADMIN
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
