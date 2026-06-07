"""Gateway management REST endpoints.

CRUD for gateways, per-gateway TCP connectivity test, start/stop control,
and site configuration.  Also provides gateway-scoped versions of existing
endpoints (points, command, models, battery limits).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.publish.command_handler import DEFAULT_MAX_POWER_W
from franklinwh_bridge.store.db import (
    create_gateway,
    delete_gateway,
    get_gateway,
    get_gateways,
    get_site_config,
    update_gateway,
    update_site_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gateways"])


def _registry(request: Request):
    reg = getattr(request.app.state, "registry", None)
    if reg is None:
        raise HTTPException(503, "Gateway registry not available")
    return reg


# ── Site config ───────────────────────────────────────────────


class SiteConfigUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    meter_number: str | None = None
    account_number: str | None = None
    ac_service_type: int | None = Field(default=None, ge=1, le=3)
    aggregate_entities: bool | None = None


@router.get("/site")
async def get_site(request: Request):
    """Return site configuration."""
    db: aiosqlite.Connection = request.app.state.db
    return await get_site_config(db)


@router.get("/site/status")
async def get_site_status(request: Request):
    """Return aggregated site-level status from all gateways."""
    aggregator = getattr(request.app.state, "site_aggregator", None)
    if aggregator is None:
        return {"gateway_count": 0, "points": {}}
    return {
        "gateway_count": aggregator.gateway_count,
        "points": aggregator.site_points,
    }


@router.patch("/site")
async def patch_site(body: SiteConfigUpdate, request: Request):
    """Update site configuration."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "aggregate_entities" in updates:
        updates["aggregate_entities"] = int(updates["aggregate_entities"])
    return await update_site_config(db, **updates)


# ── Gateway CRUD ──────────────────────────────────────────────


class GatewayCreate(BaseModel):
    gateway_id: str = Field(..., min_length=1, max_length=63)
    name: str = Field(..., min_length=1, max_length=120)
    host: str = Field(..., min_length=1)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)
    description: str = ""
    poll_interval: int = Field(default=10, ge=1, le=300)


class GatewayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    unit_id: int | None = Field(default=None, ge=1, le=247)
    description: str | None = None
    poll_interval: int | None = Field(default=None, ge=1, le=300)
    enabled: bool | None = None


@router.get("/gateways")
async def list_gateways(request: Request):
    """List all gateways with live status from the registry."""
    db: aiosqlite.Connection = request.app.state.db
    db_rows = await get_gateways(db)

    registry = getattr(request.app.state, "registry", None)
    gateways = []
    for row in db_rows:
        gw_id = row["id"]
        entry = dict(row)
        # Merge live status from registry if instance exists
        if registry:
            inst = registry.get(gw_id)
            if inst:
                entry.update({
                    "connected": inst.status.connected,
                    "polling": inst.status.polling,
                    "health": inst.status.health,
                    "serial": inst.status.serial or entry.get("serial"),
                    "model": inst.status.model or entry.get("model"),
                    "firmware": inst.status.firmware or entry.get("firmware"),
                    "ac_type": inst.status.ac_type,
                    "last_poll_ts": (
                        inst.poller.state.last_poll_ts
                        if inst.poller else None
                    ),
                    "last_error": inst.status.last_error,
                })
            else:
                entry.update({
                    "connected": False,
                    "polling": False,
                    "health": "stopped" if entry.get("enabled") else "disabled",
                })
        gateways.append(entry)

    return {"gateways": gateways}


@router.post("/gateways", status_code=201)
async def add_gateway(body: GatewayCreate, request: Request):
    """Add a new gateway."""
    db: aiosqlite.Connection = request.app.state.db
    existing = await get_gateway(db, body.gateway_id)
    if existing:
        raise HTTPException(409, f"Gateway '{body.gateway_id}' already exists")

    gw = await create_gateway(
        db,
        gateway_id=body.gateway_id,
        name=body.name,
        host=body.host,
        port=body.port,
        unit_id=body.unit_id,
        description=body.description,
        poll_interval=body.poll_interval,
    )
    return gw


@router.get("/gateways/{gw_id}")
async def get_single_gateway(gw_id: str, request: Request):
    """Get a single gateway with live status."""
    db: aiosqlite.Connection = request.app.state.db
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    entry = dict(row)
    registry = getattr(request.app.state, "registry", None)
    if registry:
        inst = registry.get(gw_id)
        if inst:
            entry.update(inst.to_dict())
    return entry


@router.patch("/gateways/{gw_id}")
async def patch_gateway(gw_id: str, body: GatewayUpdate, request: Request):
    """Update gateway configuration."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    if not updates:
        row = await get_gateway(db, gw_id)
        if row is None:
            raise HTTPException(404, f"Gateway '{gw_id}' not found")
        return row

    result = await update_gateway(db, gw_id, **updates)
    if result is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")
    return result


@router.delete("/gateways/{gw_id}")
async def remove_gateway(gw_id: str, request: Request):
    """Remove a gateway. Default gateway cannot be removed."""
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)

    # Stop instance if running
    if registry.get(gw_id):
        await registry.stop_gateway(gw_id)

    try:
        ok = await delete_gateway(db, gw_id)
    except ValueError as e:
        raise HTTPException(403, str(e)) from e
    if not ok:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")
    return {"deleted": True}


# ── Gateway lifecycle ─────────────────────────────────────────


@router.post("/gateways/{gw_id}/start")
async def start_gateway_endpoint(gw_id: str, request: Request):
    """Start polling a gateway."""
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)

    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    # Enable in DB if disabled
    if not row.get("enabled"):
        await update_gateway(db, gw_id, enabled=1)

    inst = await registry.start_gateway(gw_id)
    if inst is None:
        raise HTTPException(500, f"Failed to start gateway '{gw_id}'")
    return {"started": True, "gateway_id": gw_id}


@router.post("/gateways/{gw_id}/stop")
async def stop_gateway_endpoint(gw_id: str, request: Request):
    """Stop polling a gateway (does not disable in DB)."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")
    await registry.stop_gateway(gw_id)
    return {"stopped": True, "gateway_id": gw_id}


# ── TCP connectivity test ─────────────────────────────────────


@router.post("/gateways/{gw_id}/test")
async def test_gateway_tcp(gw_id: str, request: Request):
    """Test TCP connectivity to a gateway's host:port."""
    db: aiosqlite.Connection = request.app.state.db
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    host = row["host"]
    port = row["port"]

    def _tcp_connect() -> float:
        t0 = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((host, port))
        finally:
            sock.close()
        return time.monotonic() - t0

    try:
        elapsed = await asyncio.wait_for(
            asyncio.to_thread(_tcp_connect), timeout=5,
        )
        return {
            "ok": True,
            "gateway_id": gw_id,
            "host": host,
            "port": port,
            "latency_ms": round(elapsed * 1000, 1),
        }
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return {
            "ok": False,
            "gateway_id": gw_id,
            "host": host,
            "port": port,
            "latency_ms": None,
            "error": str(exc) or type(exc).__name__,
        }


# ── Gateway-scoped endpoints ─────────────────────────────────


@router.get("/gateways/{gw_id}/points")
async def get_gateway_points(gw_id: str, request: Request):
    """Get current points for a specific gateway."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")

    last = inst.sample_bus.last_sample
    if last is None:
        return {"gateway_id": gw_id, "points": {}, "ts": None}

    points = dict(last.points)
    if inst.command_handler:
        points.update(inst.command_handler.virtual_points)
    return {
        "gateway_id": gw_id,
        "points": points,
        "ts": last.ts,
        "quality": last.quality,
    }


class GatewayCommandRequest(BaseModel):
    slug: str
    value: str


@router.post("/gateways/{gw_id}/command")
async def send_gateway_command(
    gw_id: str, body: GatewayCommandRequest, request: Request,
):
    """Dispatch a control command to a specific gateway."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")
    if inst.command_handler is None:
        return {
            "ok": False, "slug": body.slug,
            "result": "Command handler not available",
        }

    await inst.command_handler.handle_command(body.slug, body.value)
    return {
        "ok": True,
        "slug": body.slug,
        "gateway_id": gw_id,
        "result": inst.command_handler.state.last_result or "Sent",
    }


@router.get("/gateways/{gw_id}/battery/limits")
async def get_gateway_battery_limits(gw_id: str, request: Request):
    """Return battery power limits for a specific gateway."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")
    if inst.command_handler is None:
        return {
            "max_charge_w": DEFAULT_MAX_POWER_W,
            "max_discharge_w": DEFAULT_MAX_POWER_W,
            "source": "default",
        }
    return inst.command_handler.power_limits
