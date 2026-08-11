"""Multi-HA REST — manage Home Assistant instances the Bridge reads entities from.

CRUD for the ``ha_instances`` table plus a live entity catalog and a
test-connection probe. HA entity states surface as ``ha:<instance>:<entity>``
automation condition sensors (merged into ``/api/sensors`` and the engine's
snapshot); this router is the config plane for that.

After any CRUD change we ``reload()`` the running ``HaRegistry`` so the new
instance set takes effect without a restart.
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.gateway.ha import HaInstance
from franklinwh_bridge.store.db import (
    create_ha_instance,
    delete_ha_instance,
    get_ha_instance,
    get_ha_instances,
    update_ha_instance,
)

router = APIRouter(prefix="/api/ha", tags=["home-assistant"])


class HaInstanceCreate(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    token: str | None = None
    is_default: bool = False
    enabled: bool = True


class HaInstanceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    token: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class HaTestBody(BaseModel):
    base_url: str = Field(min_length=1)
    token: str | None = None


class HaExposeBody(BaseModel):
    instance_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    exposed: bool


def _registry(request: Request):
    return getattr(request.app.state, "ha_registry", None)


async def _reload(request: Request) -> None:
    reg = _registry(request)
    if reg is not None:
        await reg.reload()


def _redact(row: dict) -> dict:
    """Never echo the token back; expose only whether one is set."""
    out = {k: v for k, v in row.items() if k != "token"}
    out["has_token"] = bool(row.get("token"))
    return out


@router.get("/instances")
async def list_instances(request: Request):
    """Configured HA instances (tokens redacted) + live connection status."""
    db: aiosqlite.Connection = request.app.state.db
    rows = await get_ha_instances(db)
    reg = _registry(request)
    status = {s["id"]: s for s in reg.status()} if reg is not None else {}
    return [{**_redact(r), "status": status.get(r["id"])} for r in rows]


@router.post("/instances", status_code=201)
async def create_instance(body: HaInstanceCreate, request: Request):
    db: aiosqlite.Connection = request.app.state.db
    row = await create_ha_instance(
        db,
        body.name,
        body.base_url,
        token=body.token,
        is_default=body.is_default,
        enabled=body.enabled,
    )
    await _reload(request)
    return _redact(row)


@router.patch("/instances/{ha_id}")
async def patch_instance(ha_id: str, body: HaInstanceUpdate, request: Request):
    db: aiosqlite.Connection = request.app.state.db
    # Only forward explicitly-set fields so a token isn't cleared by omission.
    updates = body.model_dump(exclude_unset=True)
    row = await update_ha_instance(db, ha_id, **updates)
    if row is None:
        raise HTTPException(status_code=404, detail="HA instance not found")
    await _reload(request)
    return _redact(row)


@router.delete("/instances/{ha_id}")
async def remove_instance(ha_id: str, request: Request):
    db: aiosqlite.Connection = request.app.state.db
    if await get_ha_instance(db, ha_id) is None:
        raise HTTPException(status_code=404, detail="HA instance not found")
    await delete_ha_instance(db, ha_id)
    await _reload(request)
    return {"deleted": ha_id}


@router.post("/test")
async def test_connection(body: HaTestBody):
    """Probe a base_url/token WITHOUT saving — powers the config form's Test button.

    Returns ``{connected, entity_count, last_error}``.
    """
    inst = HaInstance(
        {"id": "_probe", "name": "probe", "base_url": body.base_url, "token": body.token}
    )
    await inst.refresh()
    return {
        "connected": inst.connected,
        "entity_count": len(inst._states),
        "last_error": inst.last_error,
    }


@router.get("/entities")
async def browse_entities(
    request: Request,
    instance: str | None = None,
    domain: str | None = None,
    search: str | None = None,
    exposed: bool | None = None,
    page: int = 1,
    page_size: int = 50,
):
    """Browse ALL HA entities across instances (with an ``exposed`` flag),
    filtered + paginated — backs the HA Entities tab.

    Filters: ``instance`` (id), ``domain``, ``search`` (substring of entity_id or
    friendly name), ``exposed`` (true/false). Returns
    ``{total, page, page_size, exposed_count, entities:[...]}``.
    """
    reg = _registry(request)
    rows = reg.browse() if reg is not None else []
    exposed_count = sum(1 for r in rows if r["exposed"])

    if instance:
        rows = [r for r in rows if r["instance"] == instance]
    if domain:
        rows = [r for r in rows if r["domain"] == domain]
    if exposed is not None:
        rows = [r for r in rows if r["exposed"] is exposed]
    if search:
        q = search.lower()
        rows = [r for r in rows if q in r["entity_id"].lower() or q in r["friendly_name"].lower()]

    rows.sort(key=lambda r: (r["instance_name"], r["entity_id"]))
    total = len(rows)
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "exposed_count": exposed_count,
        "entities": rows[start : start + page_size],
    }


@router.get("/domains")
async def list_domains(request: Request):
    """Distinct entity domains across all instances (for the filter dropdown)."""
    reg = _registry(request)
    return reg.domains() if reg is not None else []


@router.post("/entities/expose")
async def expose_entity(body: HaExposeBody, request: Request):
    """Add/remove an entity from an instance's allowlist (persists + applies live)."""
    reg = _registry(request)
    if reg is None:
        raise HTTPException(status_code=503, detail="multi-HA not running")
    await reg.set_exposed(body.instance_id, body.entity_id, body.exposed)
    return {"instance_id": body.instance_id, "entity_id": body.entity_id, "exposed": body.exposed}
