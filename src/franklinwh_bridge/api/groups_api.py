"""Publishing groups REST endpoints.

Provides CRUD for publishing groups and their entity members, plus a
catalog of all available entity definitions for the Settings UI.
"""

from __future__ import annotations

import logging
import re

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import get_current_user
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.store.db import (
    add_group_member,
    add_group_point_member,
    create_publishing_group,
    delete_publishing_group,
    get_disabled_entity_slugs,
    get_group_members,
    get_group_point_members,
    get_publishing_group,
    get_publishing_groups,
    remove_group_member,
    remove_group_point_member,
    set_group_members,
    update_publishing_group,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/groups", tags=["groups"])

_CURRENT_USER = Depends(get_current_user)  # hoisted: Depends() in a default trips B008


def _audit(user: dict | None, what: str) -> None:
    """One app-log line per change that adds or removes HA entities (#296 item 39)."""
    logger.info("Publishing groups: %s (by %s)", what, (user or {}).get("username") or "unknown")

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


class PointMember(BaseModel):
    ref: str = Field(..., pattern=r"^\d+\.[A-Za-z0-9_]+$")  # "model.point"
    disp_name: str | None = Field(default=None, max_length=120)
    disp_unit: str | None = Field(default=None, max_length=24)


@router.get("")
async def list_groups(request: Request):
    """List all publishing groups with member counts."""
    db: aiosqlite.Connection = request.app.state.db
    groups = await get_publishing_groups(db)
    return {"groups": groups}


@router.get("/entities")
async def list_entities():
    """Return the full entity catalog — all available entity definitions.

    Used by the Settings UI to populate group member pickers.  Each entry
    includes slug, name, ha_type, state_group, and whether it's a control
    entity.
    """
    return {
        "entities": [
            {
                "slug": e.slug,
                "name": e.name,
                "ha_type": e.ha_type,
                "state_group": e.state_group,
                "unit": e.unit,
                "is_control": e.is_control,
                "source": e.source,
                "phase": e.phase,
            }
            for e in BRIDGE_ENTITIES
        ],
        "total": len(BRIDGE_ENTITIES),
    }


@router.get("/disabled")
async def list_disabled(request: Request):
    """Return slugs of entities disabled by group settings."""
    db: aiosqlite.Connection = request.app.state.db
    disabled = await get_disabled_entity_slugs(db)
    return {"disabled_slugs": sorted(disabled)}


@router.post("", status_code=201)
async def create_group(body: GroupCreate, request: Request, user: dict | None = _CURRENT_USER):
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
    _audit(user, f"group '{body.slug}' created, enabled={body.enabled}, "
                 f"{len(body.members)} members")
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
async def patch_group(
    slug: str, body: GroupUpdate, request: Request, user: dict | None = _CURRENT_USER
):
    """Update name, description, or enabled state of a group."""
    db: aiosqlite.Connection = request.app.state.db
    existing = await get_publishing_group(db, slug)
    if existing is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    updated = await update_publishing_group(
        db, slug, name=body.name, description=body.description, enabled=body.enabled
    )
    await _sync_publisher(request)
    if body.enabled is not None and bool(body.enabled) != bool(existing.get("enabled")):
        _audit(user, f"group '{slug}' {'enabled' if existing.get('enabled') else 'disabled'}"
                     f" → {'enabled' if body.enabled else 'disabled'}")
    return updated


@router.delete("/{slug}")
async def delete_group(slug: str, request: Request, user: dict | None = _CURRENT_USER):
    """Delete a custom group. Default groups cannot be deleted."""
    db: aiosqlite.Connection = request.app.state.db
    try:
        ok = await delete_publishing_group(db, slug)
    except ValueError as e:
        raise HTTPException(403, str(e)) from e
    if not ok:
        raise HTTPException(404, f"Group '{slug}' not found")
    await _sync_publisher(request)
    _audit(user, f"group '{slug}' deleted")
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
async def replace_members(
    slug: str, body: MemberUpdate, request: Request, user: dict | None = _CURRENT_USER
):
    """Replace all members of a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    old = set(group.get("members") or [])
    members = await set_group_members(db, slug, body.members)
    await _sync_publisher(request)
    added, removed = sorted(set(members) - old), sorted(old - set(members))
    if added or removed:
        _audit(user, f"group '{slug}' members: added {added or 'none'}, "
                     f"removed {removed or 'none'}")
    return {"members": members}


@router.post("/{slug}/members")
async def add_member(
    slug: str, body: SingleMember, request: Request, user: dict | None = _CURRENT_USER
):
    """Add an entity to a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    await add_group_member(db, slug, body.entity_slug)
    members = await get_group_members(db, slug)
    await _sync_publisher(request)
    _audit(user, f"group '{slug}' member added: {body.entity_slug}")
    return {"members": members}


@router.delete("/{slug}/members/{entity_slug}")
async def delete_member(
    slug: str, entity_slug: str, request: Request, user: dict | None = _CURRENT_USER
):
    """Remove an entity from a group."""
    db: aiosqlite.Connection = request.app.state.db
    group = await get_publishing_group(db, slug)
    if group is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    await remove_group_member(db, slug, entity_slug)
    members = await get_group_members(db, slug)
    await _sync_publisher(request)
    _audit(user, f"group '{slug}' member removed: {entity_slug}")
    return {"members": members}


# ── Promoted catalog points (Publishing Groups P1b) ──────────────

@router.get("/{slug}/points")
async def list_group_points(slug: str, request: Request):
    """List the catalog points promoted into a group."""
    db: aiosqlite.Connection = request.app.state.db
    if await get_publishing_group(db, slug) is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    return {"points": await get_group_point_members(db, slug)}


@router.post("/{slug}/points", status_code=201)
async def add_group_point(
    slug: str, body: PointMember, request: Request, user: dict | None = _CURRENT_USER
):
    """Promote a catalog point ("model.point") into a group."""
    db: aiosqlite.Connection = request.app.state.db
    if await get_publishing_group(db, slug) is None:
        raise HTTPException(404, f"Group '{slug}' not found")
    await add_group_point_member(db, slug, body.ref, body.disp_name, body.disp_unit)
    await _sync_publisher(request)
    _audit(user, f"group '{slug}' point promoted: {body.ref}")
    return {"added": body.ref}


@router.delete("/{slug}/points")
async def remove_group_point(
    slug: str, request: Request, ref: str, user: dict | None = _CURRENT_USER
):
    """Remove a promoted point from a group (ref as ?ref=model.point)."""
    db: aiosqlite.Connection = request.app.state.db
    await remove_group_point_member(db, slug, ref)
    await _sync_publisher(request)
    _audit(user, f"group '{slug}' point removed: {ref}")
    return {"removed": ref}
