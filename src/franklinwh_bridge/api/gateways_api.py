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

from franklinwh_bridge.gateway.phase_detect import detect_phases, phase_matches
from franklinwh_bridge.publish.command_handler import DEFAULT_MAX_POWER_W
from franklinwh_bridge.store.db import (
    create_gateway,
    create_service,
    delete_gateway,
    delete_service,
    get_gateway,
    get_gateways,
    get_services,
    get_site_config,
    update_gateway,
    update_service,
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
        "ts": aggregator.last_update or None,
    }


@router.patch("/site")
async def patch_site(body: SiteConfigUpdate, request: Request):
    """Update site configuration."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "aggregate_entities" in updates:
        updates["aggregate_entities"] = int(updates["aggregate_entities"])
    return await update_site_config(db, **updates)


# ── Electricity Utility Services ──────────────────────────────


class ServiceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    meter_number: str = Field(default="", max_length=120)
    account: str = Field(default="", max_length=120)
    ac_service: int = Field(default=1, ge=1, le=3)
    rated_amps: int = Field(default=0, ge=0, le=10000)


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    meter_number: str | None = Field(default=None, max_length=120)
    account: str | None = Field(default=None, max_length=120)
    ac_service: int | None = Field(default=None, ge=1, le=3)
    rated_amps: int | None = Field(default=None, ge=0, le=10000)


@router.get("/services")
async def list_services(request: Request):
    """List all electricity utility services."""
    db: aiosqlite.Connection = request.app.state.db
    return {"services": await get_services(db)}


@router.post("/services", status_code=201)
async def add_service(body: ServiceCreate, request: Request):
    """Create a new utility service."""
    db: aiosqlite.Connection = request.app.state.db
    return await create_service(
        db,
        name=body.name,
        meter_number=body.meter_number,
        account=body.account,
        ac_service=body.ac_service,
        rated_amps=body.rated_amps,
    )


@router.patch("/services/{service_id}")
async def patch_service(service_id: str, body: ServiceUpdate, request: Request):
    """Update a utility service."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    result = await update_service(db, service_id, **updates)
    if result is None:
        raise HTTPException(404, f"Service '{service_id}' not found")
    return result


@router.delete("/services/{service_id}")
async def remove_service(service_id: str, request: Request):
    """Delete a utility service."""
    db: aiosqlite.Connection = request.app.state.db
    if not await delete_service(db, service_id):
        raise HTTPException(404, f"Service '{service_id}' not found")
    return {"deleted": True}


# ── Gateway CRUD ──────────────────────────────────────────────


class GatewayCreate(BaseModel):
    gateway_id: str = Field(..., min_length=1, max_length=63)
    name: str = Field(..., min_length=1, max_length=120)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)
    description: str = ""
    poll_interval: int = Field(default=10, ge=1, le=300)
    mock: bool = False


class GatewayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    unit_id: int | None = Field(default=None, ge=1, le=247)
    description: str | None = None
    poll_interval: int | None = Field(default=None, ge=1, le=300)
    enabled: bool | None = None
    # Layer-2 linkage. service_id='' clears the link; phase in all|L1|L2|L3|combo.
    service_id: str | None = Field(default=None, max_length=63)
    phase: str | None = Field(default=None, pattern=r"^(all|L[123](\+L[123])*)$")
    phase_view: str | None = Field(default=None, pattern=r"^(both|aggregate|per_phase)$")


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

    # Conflict guards apply to REAL gateways only (mocks never open a Modbus
    # connection, so they can't contend or clash).
    if not body.mock:
        if not body.host:
            raise HTTPException(400, "Host is required for a real gateway")
        for row in await get_gateways(db):
            if row.get("mock"):
                continue
            if (
                row["host"] == body.host
                and row["port"] == body.port
                and row.get("unit_id", 1) == body.unit_id
            ):
                raise HTTPException(
                    409,
                    f"Gateway '{row['id']}' already polls {body.host}:{body.port} "
                    f"unit {body.unit_id}. Two real gateways can't share one "
                    f"aGate's Modbus session — point this one at a different "
                    f"device, or mark it as a mock.",
                )

    gw = await create_gateway(
        db,
        gateway_id=body.gateway_id,
        name=body.name,
        host=body.host or ("mock" if body.mock else ""),
        port=body.port,
        unit_id=body.unit_id,
        description=body.description,
        poll_interval=body.poll_interval,
        mock=body.mock,
    )

    # Onboard immediately so the gateway starts polling without an app restart.
    # start_gateway is non-blocking (connect/discover runs in a background
    # task); a failed connection just surfaces the gateway as offline rather
    # than failing the create.
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            await registry.start_gateway(body.gateway_id)
        except Exception as exc:
            logger.warning(
                "Gateway %s created but failed to start: %s", body.gateway_id, exc
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

    # Apply a phase-view change to the live publisher (default gateway only —
    # that's the one wired to the MQTT publisher today). Re-publishes discovery.
    if "phase_view" in updates and gw_id == "default":
        publisher = getattr(request.app.state, "mqtt_publisher", None)
        if publisher is not None:
            publisher.set_phase_view(updates["phase_view"])

    return result


@router.delete("/gateways/{gw_id}")
async def remove_gateway(gw_id: str, request: Request):
    """Remove a gateway. Default gateway cannot be removed."""
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)

    # Stop instance if running
    if registry.get(gw_id):
        await registry.stop_gateway(gw_id)

    # Offboard: drop the gateway's cached samples so it no longer skews the
    # site aggregate after removal.
    aggregator = getattr(request.app.state, "site_aggregator", None)
    if aggregator is not None:
        aggregator.clear_gateway(gw_id)

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

    # Enable + clear any prior "user stopped" pause so it auto-starts on boot.
    await update_gateway(db, gw_id, enabled=1, autostart=1)

    inst = await registry.start_gateway(gw_id)
    if inst is None:
        raise HTTPException(500, f"Failed to start gateway '{gw_id}'")
    return {"started": True, "gateway_id": gw_id}


@router.post("/gateways/{gw_id}/stop")
async def stop_gateway_endpoint(gw_id: str, request: Request):
    """Stop polling a gateway and remember the stop across restarts.

    Sets ``autostart=0`` so the gateway is not re-started on the next app
    boot.  ``enabled`` stays 1 — this is a user pause, not an admin disable,
    so the gateway remains configured and can be started again.  This is what
    keeps a stopped (esp. mock) gateway from self-restarting on reboot.
    """
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    await update_gateway(db, gw_id, autostart=0)
    if registry.get(gw_id):
        await registry.stop_gateway(gw_id)
    return {"stopped": True, "gateway_id": gw_id}


@router.get("/gateways/{gw_id}/detect-phases")
async def detect_gateway_phases(gw_id: str, request: Request):
    """Auto-detect the gateway's wired phase(s) from its 701 registers.

    Reads the gateway's latest cached points (no extra Modbus traffic) and
    infers connected/utilised phases. Also reports whether the result matches
    the gateway's currently declared ``phase``.
    """
    db: aiosqlite.Connection = request.app.state.db
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    # Prefer the gateway's own sample bus (carries the full 701.* per-phase set,
    # same source as /api/points); fall back to the site aggregator's cache.
    points: dict = {}
    registry = getattr(request.app.state, "registry", None)
    inst = registry.get(gw_id) if registry else None
    if inst is not None and inst.sample_bus.last_sample is not None:
        points = dict(inst.sample_bus.last_sample.points)
    else:
        aggregator = getattr(request.app.state, "site_aggregator", None)
        if aggregator is not None:
            points = aggregator.get_gateway_points(gw_id)
    result = detect_phases(points)
    declared = row.get("phase", "all")
    result["declared"] = declared
    result["matches_declared"] = phase_matches(declared, result["detected"])
    return result


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
        "ok": inst.command_handler.state.last_success,
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
