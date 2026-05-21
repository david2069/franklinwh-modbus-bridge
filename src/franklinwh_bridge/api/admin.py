"""Admin REST routes — the internal gateway."""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from franklinwh_bridge.modbus.catalog import capture_catalog, load_catalog

router = APIRouter(prefix="/api", tags=["admin"])


class ConfigUpdate(BaseModel):
    value: Any


@router.get("/config/{section}")
async def get_config(section: str, request: Request):
    db: aiosqlite.Connection = request.app.state.db
    if section == "all":
        result = {}
        async with db.execute("SELECT key, value FROM app_config") as cursor:
            async for row in cursor:
                result[row[0]] = row[1]
        return result

    async with db.execute(
        "SELECT value FROM app_config WHERE key = ?", (section,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, f"Config key '{section}' not found")
    return {"key": section, "value": row[0]}


@router.put("/config/{key}")
async def set_config(key: str, body: ConfigUpdate, request: Request):
    db: aiosqlite.Connection = request.app.state.db
    await db.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(body.value)),
    )
    await db.commit()
    return {"key": key, "value": str(body.value)}


@router.get("/models")
async def get_models(request: Request):
    db: aiosqlite.Connection = request.app.state.db
    gateway_id = request.app.state.gateway_id
    catalog = await load_catalog(db, gateway_id)

    models: dict[int, dict] = {}
    for rec in catalog:
        mid = rec["model_id"]
        if mid not in models:
            models[mid] = {"model_id": mid, "label": rec["model_label"], "points": []}
        models[mid]["points"].append({
            "name": rec["point_name"],
            "type": rec["type"],
            "unit": rec["unit"],
            "address": rec["address"],
            "writable": rec["writable"],
        })

    return {"gateway_id": gateway_id, "models": list(models.values())}


@router.post("/models/refresh")
async def refresh_models(request: Request):
    db: aiosqlite.Connection = request.app.state.db
    gateway_id = request.app.state.gateway_id
    reader_fn = getattr(request.app.state, "reader_fn", None)

    if reader_fn is None:
        raise HTTPException(503, "Model reader not configured")

    device_info, error = await reader_fn()
    if error:
        raise HTTPException(502, f"Capture failed: {error}")

    catalog_hash, diff = await capture_catalog(device_info, db, gateway_id)

    return {
        "hash": catalog_hash,
        "changes": diff.has_changes,
        "added_models": diff.added_models,
        "removed_models": diff.removed_models,
        "added_points": diff.added_points,
        "removed_points": diff.removed_points,
        "changed_points": diff.changed_points,
    }


@router.get("/points")
async def get_points(request: Request):
    sample_bus = request.app.state.sample_bus
    last = sample_bus.last_sample
    if last is None:
        return {"gateway_id": request.app.state.gateway_id, "points": {}, "ts": None}
    return {
        "gateway_id": last.gateway_id,
        "points": last.points,
        "ts": last.ts,
        "quality": last.quality,
    }


@router.get("/points/{point_id}")
async def get_point(point_id: str, request: Request):
    sample_bus = request.app.state.sample_bus
    last = sample_bus.last_sample
    if last is None or point_id not in last.points:
        raise HTTPException(404, f"Point '{point_id}' not found")
    return {
        "point_id": point_id,
        "value": last.points[point_id],
        "ts": last.ts,
        "quality": last.quality,
    }


@router.get("/logs")
async def get_logs(request: Request, limit: int = 100):
    log_buffer = getattr(request.app.state, "log_buffer", None)
    if log_buffer is None:
        return {"logs": []}
    entries = list(log_buffer)[-limit:]
    return {"logs": entries}
