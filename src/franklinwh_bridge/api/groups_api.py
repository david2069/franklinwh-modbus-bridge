"""Publishing groups REST endpoints."""

from __future__ import annotations

import re

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.store.db import (
    add_group_member,
    create_publishing_group,
    delete_publishing_group,
    get_disabled_entity_slugs,
    get_group_members,
    get_publishing_group,
    get_publishing_groups,
    remove_group_member,
    set_group_members,
    update_publishing_group,
)

router = APIRouter(prefix="/api/groups", tags=["groups"])

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


async def _sync_publisher(request: Request) -> None:
    """Notify the MQTT publisher to re-sync group filters."""
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    if publisher is not None:
        db: aiosqlite.Connection = request.app.state.db
        await publisher.sync_groups(db)


class GroupCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=63)
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    enabled: bool = True
    members: list[str] = Field(default_factory=list)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class MemberUpdate(BaseModel):
    members: list[str]


class SingleMember(BaseModel):
    entity_slug: str


@router.get("")
async def list_groups(request: Request):
    """List all publishing groups with member counts."""
    db: aiosqlite.Connection = request.app.state.db
    groups = await get_publishing_groups(db)
    return {"groups": groups}


@router.get("/disabled")
async def list_disabled(request: Request):
    """Return slugs of entities disabled by group settings."""
    db: aiosqlite.Connection = request.app.state.db
    disabled = await get_disabled_entity_slugs(db)
    return {"disabled_slugs": sorted(disabled)}


@router.post("", status_code=201)
async def create_group(body: GroupCreate, request: Request):
    """Create a new custom publishing group."""
    if not SLUG_PATTERN.match(body.slug):
        raise HTTPException(
            422,
            "Slug must be 2-63 chars, lowercase letters/digits/underscores, "
            "starting with a letter.",
        )
    db: aiosqlite.Connection = request.app.state.db
    existing = await get_publishing_group(db, body.slug)
    if existing is not None:
        raise HTTPException(409, f"Group '{body.slug}' already exists")
    group = await create_publishing_group(
        db,
        slug=body.slug,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        members=body.members,
    )
    await _sync_publisher(request)
    return group


@router.get("/{slug}")
async def get_group(slug: str, request: Request):
    """Get a single group with its member list."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    return group


@router.patch("/{slug}")
async def patch_group(slug: str, body: GroupUpdate, request: Request):
    """Update name, description, or enabled state of a group."""
    db: aiosqlite.Connection = request.app.state.db
    existing = await get_publishing_group(db, slug)
    if existing is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    updated = await update_publishing_group(
        db, slug, name=body.name, description=body.description, enabled=body.enabled
    )
    await _sync_publisher(request)
    return updated


@router.delete("/{slug}")
async def delete_group(slug: str, request: Request):
    """Delete a custom group. Default groups cannot be deleted."""
    db: aiosqlite.Connection = request.app.state.db
    try:
        ok = await delete_publishing_group(db, slug)
    except ValueError as e:
        raise HTTPException(403, str(e)) from e
    if not ok:
        raise HTTPException(404, f"Group '{slug}' not found")
    await _sync_publisher(request)
    return {"deleted": True}


# ── Member management ─────────────────────────────────────────


@router.get("/{slug}/members")
async def list_members(slug: str, request: Request):
    """List entity slugs in a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    return {"members": group["members"]}


@router.put("/{slug}/members")
async def replace_members(slug: str, body: MemberUpdate, request: Request):
    """Replace all members of a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    members = await set_group_members(db, slug, body.members)
    await _sync_publisher(request)
    return {"members": members}


@router.post("/{slug}/members")
async def add_member(slug: str, body: SingleMember, request: Request):
    """Add an entity to a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    await add_group_member(db, slug, body.entity_slug)
    members = await get_group_members(db, slug)
    await _sync_publisher(request)
    return {"members": members}


@router.delete("/{slug}/members/{entity_slug}")
async def delete_member(slug: str, entity_slug: str, request: Request):
    """Remove an entity from a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    await remove_group_member(db, slug, entity_slug)
    members = await get_group_members(db, slug)
    await _sync_publisher(request)
    return {"members": members}
