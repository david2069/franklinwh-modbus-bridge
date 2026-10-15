"""Admin user management — list / create / update / delete users. Admin-only.

Guards against lockout: you can't delete, disable, demote, or set an expiry on
the last enabled admin. Passwords are argon2-hashed; hashes are never returned.

Roles: admin, user, viewer, and api (#99: REST only, view + control; the web
pages refuse it). ``expires_at`` (epoch seconds, null = never) ends an account:
sign-in is refused after it and open logins end on their next request.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import auth_sessions
from franklinwh_bridge.store.db import (
    create_user,
    delete_user,
    get_user,
    get_user_by_username,
    get_users,
    update_user,
    user_expired,
)

# Every route requires an admin.
router = APIRouter(prefix="/api", tags=["users"], dependencies=[Depends(require_role("admin"))])

_ROLES = r"^(admin|user|viewer|api)$"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)
    role: str = Field(default="viewer", pattern=_ROLES)
    #: Epoch seconds after which the account can no longer sign in; None = never.
    expires_at: float | None = Field(default=None, ge=0)


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=_ROLES)
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=1)
    #: Send null to clear the expiry; leave it out to keep it unchanged.
    expires_at: float | None = Field(default=None, ge=0)


def _redact(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "password_hash"}


def _acting(request: Request) -> tuple[str | None, str | None]:
    """The admin making the change and their own login id (set by require_auth)."""
    user = getattr(request.state, "auth_user", None) or {}
    return user.get("username"), getattr(request.state, "auth_sid", None)


def _enabled_admins(users: list[dict]) -> list[dict]:
    """Admins who can still sign in: enabled and not past their expiry."""
    return [
        u for u in users if u["role"] == "admin" and u["enabled"] and not user_expired(u)
    ]


@router.get("/users")
async def list_users(request: Request):
    return [_redact(u) for u in await get_users(request.app.state.db)]


@router.post("/users", status_code=201)
async def create_new_user(body: UserCreate, request: Request):
    db = request.app.state.db
    # The lookup ignores case, so "Admin" is refused when "admin" exists:
    # two accounts that differ only in case can't be told apart at login.
    clash = await get_user_by_username(db, body.username)
    if clash is not None:
        raise HTTPException(409, f"user '{clash['username']}' already exists")
    u = await create_user(
        db, body.username, hash_password(body.password), role=body.role,
        expires_at=body.expires_at,
    )
    return _redact(u)


@router.patch("/users/{user_id}")
async def update_existing_user(user_id: str, body: UserUpdate, request: Request):
    db = request.app.state.db
    existing = await get_user(db, user_id)
    if existing is None:
        raise HTTPException(404, "user not found")

    # Lockout guard: can't demote/disable/expire the last enabled admin.
    setting_expiry = "expires_at" in body.model_fields_set and body.expires_at is not None
    removing_admin = (
        (body.role is not None and body.role != "admin")
        or body.enabled is False
        or setting_expiry
    )
    if (
        existing["role"] == "admin"
        and existing["enabled"]
        and not user_expired(existing)
        and removing_admin
        and len(_enabled_admins(await get_users(db))) <= 1
    ):
        raise HTTPException(
            400, "can't demote, disable or set an expiry on the last enabled admin"
        )

    updates: dict = {}
    if body.role is not None:
        updates["role"] = body.role
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.password:
        updates["password_hash"] = hash_password(body.password)
    if "expires_at" in body.model_fields_set:
        updates["expires_at"] = body.expires_at
    updated = await update_user(db, user_id, **updates)

    # End the account's open logins, so its pages say why rather than go blank.
    by, own_sid = _acting(request)
    if setting_expiry and body.expires_at <= time.time():
        await auth_sessions.end_user_sessions(db, user_id, "expired", by=by, request=request)
    if body.enabled is False:
        await auth_sessions.end_user_sessions(
            db, user_id, "user_disabled", by=by, request=request
        )
    elif body.password:
        # Your own other logins end too; the one you're using stays.
        await auth_sessions.end_user_sessions(
            db, user_id, "password_changed", by=by, except_sid=own_sid, request=request
        )
    return _redact(updated)


@router.delete("/users/{user_id}")
async def remove_user(user_id: str, request: Request):
    db = request.app.state.db
    existing = await get_user(db, user_id)
    if existing is None:
        raise HTTPException(404, "user not found")
    if (
        existing["role"] == "admin"
        and existing["enabled"]
        and len(_enabled_admins(await get_users(db))) <= 1
    ):
        raise HTTPException(400, "can't delete the last enabled admin")
    by, _ = _acting(request)
    await auth_sessions.end_user_sessions(db, user_id, "user_deleted", by=by, request=request)
    await delete_user(db, user_id)
    return {"deleted": user_id}
