"""Admin user management — list / create / update / delete users. Admin-only.

Guards against lockout: you can't delete, disable, or demote the last enabled
admin. Passwords are argon2-hashed; hashes are never returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store.db import (
    create_user,
    delete_user,
    get_user,
    get_user_by_username,
    get_users,
    update_user,
)

# Every route requires an admin.
router = APIRouter(prefix="/api", tags=["users"], dependencies=[Depends(require_role("admin"))])

_ROLES = r"^(admin|user|viewer)$"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)
    role: str = Field(default="viewer", pattern=_ROLES)


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=_ROLES)
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=1)


def _redact(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "password_hash"}


def _enabled_admins(users: list[dict]) -> list[dict]:
    return [u for u in users if u["role"] == "admin" and u["enabled"]]


@router.get("/users")
async def list_users(request: Request):
    return [_redact(u) for u in await get_users(request.app.state.db)]


@router.post("/users", status_code=201)
async def create_new_user(body: UserCreate, request: Request):
    db = request.app.state.db
    if await get_user_by_username(db, body.username) is not None:
        raise HTTPException(409, f"user '{body.username}' already exists")
    u = await create_user(db, body.username, hash_password(body.password), role=body.role)
    return _redact(u)


@router.patch("/users/{user_id}")
async def update_existing_user(user_id: str, body: UserUpdate, request: Request):
    db = request.app.state.db
    existing = await get_user(db, user_id)
    if existing is None:
        raise HTTPException(404, "user not found")

    # Lockout guard: can't demote/disable the last enabled admin.
    removing_admin = (body.role is not None and body.role != "admin") or body.enabled is False
    if (
        existing["role"] == "admin"
        and existing["enabled"]
        and removing_admin
        and len(_enabled_admins(await get_users(db))) <= 1
    ):
        raise HTTPException(400, "can't demote or disable the last enabled admin")

    updates: dict = {}
    if body.role is not None:
        updates["role"] = body.role
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.password:
        updates["password_hash"] = hash_password(body.password)
    return _redact(await update_user(db, user_id, **updates))


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
    await delete_user(db, user_id)
    return {"deleted": user_id}
