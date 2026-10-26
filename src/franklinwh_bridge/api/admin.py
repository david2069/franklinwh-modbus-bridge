"""Admin REST routes — the internal gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from franklinwh_bridge import __version__
from franklinwh_bridge.api.auth import require_capability
from franklinwh_bridge.gateway import link_stats
from franklinwh_bridge.modbus.catalog import capture_catalog, load_catalog
from franklinwh_bridge.store import modbus_audit
from franklinwh_bridge.store.alarms import (
    compute_grid_mode_intervals,
    pair_alarm_events,
    query_alarm_events,
)
from franklinwh_bridge.store.backup import BackupManager
from franklinwh_bridge.store.db import (
    get_catalog_points,
    get_pics_compliance,
    log_control_event,
    query_logs,
    set_pics_status,
)
from franklinwh_bridge.store.db import (
    get_gateway as get_gateway_row,
)
from franklinwh_bridge.store.metrics import (
    BUCKET_MAP,
    RANGE_MAP,
    archive_old_metrics,
    export_metrics,
    format_metrics_csv,
    get_raw_age_days,
    get_retention_days,
    get_storage_stats,
    import_metrics_csv,
    query_grid_mode_transitions,
    query_metrics,
    query_metrics_daterange,
    query_metrics_with_archive,
    set_raw_age_days,
    set_retention_days,
)
from franklinwh_bridge.support import sequence_import as si
from franklinwh_bridge.support import sunspec_export as sx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["admin"])


def _get_gateway(request: Request, gateway_id: str = "default"):
    """Resolve a GatewayInstance from the registry (or None)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    return registry.get(gateway_id)


def _get_controller(request: Request, gateway_id: str = "default"):
    """Get the controller for a gateway, falling back to app.state."""
    inst = _get_gateway(request, gateway_id)
    if inst and inst.controller:
        return inst.controller
    # Legacy fallback
    return getattr(request.app.state, "controller", None)


def _get_command_handler(request: Request, gateway_id: str = "default"):
    """Get the command handler for a gateway, falling back to app.state."""
    inst = _get_gateway(request, gateway_id)
    if inst and inst.command_handler:
        return inst.command_handler
    # Legacy fallback
    return getattr(request.app.state, "command_handler", None)


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


async def _gateway_address(request: Request, gateway_id: str) -> dict:
    """A gateway's address as configured — its stored row, not the environment.

    This used to return MODBUS_HOST from the environment, so a gateway set up
    in the setup wizard or Settings showed no host at all: the Dashboard's
    "IP --" and the SunSpec Explorer's "GATEWAY :502".
    """
    db = getattr(request.app.state, "db", None)
    row = await get_gateway_row(db, gateway_id) if db is not None else None
    if row is not None:
        return {
            "gateway_id": gateway_id,
            "name": row.get("name"),
            "host": row.get("host") or "",
            "port": row.get("port") or 502,
            "unit_id": row.get("unit_id") or 1,
            "poll_interval": row.get("poll_interval") or 10,
            "mock": bool(row.get("mock")),
        }
    if gateway_id != "default":
        raise HTTPException(404, f"Gateway '{gateway_id}' not found")
    config = getattr(request.app.state, "config", None)
    if config is None:
        return {"gateway_id": gateway_id, "host": "", "port": 502, "unit_id": 1,
                "poll_interval": 10, "mock": False}
    gw = config.settings.gateway
    return {"gateway_id": gateway_id, "host": gw.host, "port": gw.port,
            "unit_id": gw.unit_id, "poll_interval": gw.poll_interval, "mock": False}


@router.get("/gateway")
async def get_gateway(request: Request, gateway_id: str = "default"):
    """The address of one gateway (the primary one unless ``gateway_id``)."""
    return await _gateway_address(request, gateway_id)


@router.get("/gateway/test")
async def test_gateway(request: Request, gateway_id: str = "default") -> dict:
    """Test TCP connectivity to a gateway's configured host:port."""
    gw = await _gateway_address(request, gateway_id)
    host, port = gw["host"], gw["port"]
    if gw["mock"] or not host:
        return {"ok": False, "host": host, "port": port, "latency_ms": None,
                "error": "demo gateway — nothing to connect to" if gw["mock"]
                else "no address set"}

    def _tcp_connect() -> float:
        """Blocking TCP connect; returns elapsed seconds."""
        t0 = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        link_stats.note_connection(gateway_id, "probe")
        try:
            with link_stats.track(gateway_id, "probe", "test"):
                sock.connect((host, port))
        finally:
            sock.close()
        return time.monotonic() - t0

    try:
        elapsed = await asyncio.wait_for(asyncio.to_thread(_tcp_connect), timeout=5)
        return {
            "ok": True,
            "host": host,
            "port": port,
            "latency_ms": round(elapsed * 1000, 1),
        }
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "latency_ms": None,
            "error": str(exc) or type(exc).__name__,
        }


@router.get("/models")
async def get_models(request: Request, gateway_id: str = "default"):
    """The SunSpec catalog captured for a gateway (the primary one unless named)."""
    db: aiosqlite.Connection = request.app.state.db
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
            "label": rec.get("label"),
            "description": rec.get("description"),
            "access": rec.get("access", "R"),
            "scale_factor": rec.get("scale_factor"),
            "size": rec.get("size"),
            "symbols": rec.get("symbols"),
        })

    return {"gateway_id": gateway_id, "models": list(models.values())}


@router.get("/catalog/points")
async def list_catalog_points(request: Request, gateway_id: str = "default"):
    """Flat list of catalog points with metadata + whether each is published
    (promoted into an enabled group). Powers the Explorer → group picker."""
    db: aiosqlite.Connection = request.app.state.db
    return {"gateway_id": gateway_id, "points": await get_catalog_points(db, gateway_id)}


class SunSpecExport(BaseModel):
    """A SunSpec export, ready to preview, copy, download or put in a Gist."""

    filename: str
    media_type: str
    content: str
    model_count: int
    point_count: int
    serial_redacted: bool


@router.get("/models/export", response_model=SunSpecExport)
async def export_models(
    request: Request,
    gateway_id: str = "default",
    models: str = "",
    format: str = "json",
    redact_serial: bool = True,
) -> SunSpecExport:
    """Export a gateway's SunSpec models with their current values (#228).

    ``models``: comma-separated model ids; empty for every model.
    ``format``: ``json`` or ``csv``. ``redact_serial`` (default true) replaces
    the serial number, in model 1's ``SN`` and anywhere else it appears, with
    ``<serial>``. The gateway's address and name are never included.
    """
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be json or csv")
    try:
        model_ids = [int(m) for m in models.split(",") if m.strip()] or None
    except ValueError as exc:
        raise HTTPException(400, "models must be comma-separated model ids") from exc

    db: aiosqlite.Connection = request.app.state.db
    catalog = await load_catalog(db, gateway_id)
    if not catalog:
        raise HTTPException(404, f"No SunSpec catalog for gateway '{gateway_id}' yet")

    inst = _get_gateway(request, gateway_id)
    last = inst.sample_bus.last_sample if inst is not None else None
    points = dict(last.points) if last is not None else {}
    row = await get_gateway_row(db, gateway_id) or {}
    status = getattr(inst, "status", None)
    serials = [row.get("serial"), getattr(status, "serial", None),
               points.get("serial"), points.get("1.SN")]
    gateway = {
        "device_type": row.get("device_type") or "agate",
        "model": getattr(status, "model", None) or row.get("model"),
        "firmware": getattr(status, "firmware", None) or row.get("firmware"),
    }
    try:
        export = sx.build_export(
            catalog, points, model_ids=model_ids, redact_serial=redact_serial,
            serials=serials, gateway=gateway, version=__version__,
        )
    except KeyError as exc:
        raise HTTPException(404, f"Model(s) not in this gateway's catalog: {exc.args[0]}") from exc

    if format == "csv":
        content, media_type = sx.render_csv(export), "text/csv"
    else:
        content, media_type = sx.render_json(export), "application/json"
    return SunSpecExport(
        filename=sx.filename(export, format, all_models=model_ids is None),
        media_type=media_type,
        content=content,
        model_count=export["model_count"],
        point_count=export["point_count"],
        serial_redacted=redact_serial,
    )


@router.post("/models/{model_id}/read")
async def read_model(model_id: int, request: Request, gateway_id: str = "default"):
    """On-demand read of a single SunSpec model from the device.

    Connects to the controller, calls model.read() to fetch live register
    values, then returns the point name→value map.  Also injects the values
    into the sample bus so the Explorer sees them immediately.
    """
    # Resolve the gateway's controller from the registry. The legacy
    # app.state.controller is unset under the multi-gateway architecture, so
    # reading it directly always failed with "No Modbus controller available".
    inst = _get_gateway(request, gateway_id)
    controller = inst.controller if inst else getattr(request.app.state, "controller", None)
    if controller is None:
        raise HTTPException(503, "No Modbus controller available")

    def _do_read() -> dict:
        was_connected = getattr(controller, "dev", None) is not None
        if not was_connected:
            controller.connect()
        try:
            model_obj = controller.get_model(model_id)
            if model_obj is None:
                return {"error": f"Model {model_id} not found on device"}
            with link_stats.track(gateway_id, "diag", "explorer"):
                model_obj.read()
            values: dict[str, Any] = {}
            pts = getattr(model_obj, "points", None)
            if pts:
                for pt_name, pt_obj in pts.items():
                    val = getattr(pt_obj, "value", None)
                    if val is not None:
                        values[f"{model_id}.{pt_name}"] = val
            return {"values": values}
        finally:
            if not was_connected:
                controller.disconnect()

    # Acquire the gateway's Modbus lock to prevent interleaving with poller/commands
    modbus_lock = inst.modbus_lock if inst else getattr(request.app.state, "modbus_lock", None)
    if modbus_lock:
        async with modbus_lock:
            result = await asyncio.to_thread(_do_read)
    else:
        result = await asyncio.to_thread(_do_read)
    if "error" in result:
        raise HTTPException(404, result["error"])

    # Inject as sticky points so they survive poll cycles (5-min TTL).
    # Use the default gateway's own bus — that's what /api/points reads now.
    sample_bus = inst.sample_bus if inst else request.app.state.sample_bus
    sample_bus.inject_sticky(result["values"])
    # Also update current sample for immediate visibility
    last = sample_bus.last_sample
    if last is not None:
        last.points.update(result["values"])

    return {
        "model_id": model_id,
        "points_read": len(result["values"]),
        "values": result["values"],
    }


@router.post("/models/refresh")
async def refresh_models(request: Request, gateway_id: str = "default"):
    """Re-read a gateway's SunSpec model chain into its catalog."""
    db: aiosqlite.Connection = request.app.state.db
    inst = _get_gateway(request, gateway_id)
    reader_fn = getattr(inst, "reader_fn", None) if inst is not None else None
    if reader_fn is None and gateway_id == "default":
        reader_fn = getattr(request.app.state, "reader_fn", None)  # legacy wiring

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
    # Scope to the DEFAULT gateway's own sample bus — never the global bus.
    # The global bus carries every gateway's samples (including mocks and any
    # second gateway on the same aGate), so reading it makes the default
    # dashboard flicker between gateways' data — partial samples blank out the
    # real fields. Reading the default instance's per-gateway bus keeps the
    # default view consistent regardless of how many other gateways exist.
    registry = getattr(request.app.state, "registry", None)
    inst = registry.get("default") if registry else None
    bus = inst.sample_bus if inst is not None else request.app.state.sample_bus
    last = bus.last_sample
    if last is None:
        return {"gateway_id": "default", "points": {}, "ts": None}

    # Merge command handler virtual points (software-tracked command state)
    points = dict(last.points)
    command_handler = (
        inst.command_handler if inst is not None
        else getattr(request.app.state, "command_handler", None)
    )
    if command_handler is not None:
        points.update(command_handler.virtual_points)

    return {
        "gateway_id": last.gateway_id,
        "points": points,
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


@router.get("/metrics")
async def get_metrics(
    request: Request,
    range: str | None = None,  # noqa: A002
    start: float | None = None,
    end: float | None = None,
    bucket: str | None = None,
    gateway_id: str | None = None,
):
    """Return power time-series for the dashboard chart.

    Supports two modes:
    - **Relative range**: ``?range=30m`` (default) — last N from now.
    - **Absolute date range**: ``?start=<epoch>&end=<epoch>`` — historical query.

    Optional ``?bucket=5m`` downsamples to the given interval.
    Valid buckets: 1m, 5m, 10m, 15m, 30m, 1h.
    """
    db: aiosqlite.Connection = request.app.state.db

    # Resolve bucket size if specified
    bucket_seconds: int | None = None
    if bucket is not None:
        if bucket not in BUCKET_MAP:
            raise HTTPException(
                400,
                f"Invalid bucket '{bucket}'. Valid: {', '.join(sorted(BUCKET_MAP))}",
            )
        bucket_seconds = BUCKET_MAP[bucket]

    # Absolute date-range mode
    if start is not None and end is not None:
        if end <= start:
            raise HTTPException(400, "end must be greater than start")
        # Safety: cap range to 90 days
        if (end - start) > 90 * 86400:
            raise HTTPException(400, "Date range cannot exceed 90 days")
        points = await query_metrics_daterange(db, start, end, bucket_seconds, gateway_id=gateway_id)
        return {"range": "custom", "start": start, "end": end, "bucket": bucket, "points": points, "gateway_id": gateway_id}

    # Relative range mode (default to 30m)
    range_key = range or "30m"
    if range_key not in RANGE_MAP:
        raise HTTPException(
            400,
            f"Invalid range '{range_key}'. Valid: {', '.join(sorted(RANGE_MAP))}",
        )
    range_s = RANGE_MAP[range_key]

    # Apply bucket override if specified, otherwise use default behaviour
    if bucket_seconds is not None:
        now = time.time()
        points = await query_metrics_daterange(
            db, now - range_s, now, bucket_seconds, gateway_id=gateway_id
        )
    elif range_s > 6 * 3600:
        points = await query_metrics_with_archive(db, range_s, gateway_id=gateway_id)
    else:
        points = await query_metrics(db, range_s, gateway_id=gateway_id)
    return {"range": range_key, "bucket": bucket, "points": points, "gateway_id": gateway_id}


@router.get("/stats/storage")
async def get_storage_info(request: Request):
    """Return DB storage statistics (row counts, sizes, time ranges)."""
    db: aiosqlite.Connection = request.app.state.db
    return await get_storage_stats(db)


@router.post("/metrics/archive")
async def run_archive(request: Request):
    """Manually trigger metrics archival (downsample old raw data)."""
    db: aiosqlite.Connection = request.app.state.db
    archived = await archive_old_metrics(db)
    return {"archived_rows": archived}


@router.get("/alarm-events")
async def get_alarm_events(
    request: Request,
    start: float | None = None,
    end: float | None = None,
    range: str | None = None,
    gateway_id: str | None = None,
):
    """Return alarm events in a time window.

    Provide either ``start``+``end`` (Unix timestamps) or ``range``
    (e.g. ``30m``, ``1h``, ``24h``).  Events are sparse — only rows
    written on register state changes are returned.
    """
    db: aiosqlite.Connection = request.app.state.db
    now = time.time()

    if start is not None and end is not None:
        start_ts, end_ts = start, end
    elif range is not None:
        range_s = RANGE_MAP.get(range)
        if range_s is None:
            raise HTTPException(400, f"Unknown range '{range}'")
        start_ts, end_ts = now - range_s, now
    else:
        start_ts, end_ts = now - 1800, now  # default 30m

    events = await query_alarm_events(
        db, start_ts, end_ts, gateway_id=gateway_id
    )
    return {"events": events, "count": len(events)}


@router.get("/events")
async def get_events(
    request: Request,
    start: float | None = None,
    end: float | None = None,
    range: str | None = None,
    gateway_id: str | None = None,
):
    """Return durationed alarm + PV-Clipping events in a time window.

    Unlike ``/alarm-events`` (raw SET/CLR transitions), this pairs each
    transition into a single row with a start time, end time, and
    duration, and merges in PV-Clipping/Grid-Forming intervals derived
    from ``metrics.grid_mode``. Same ``start``+``end`` / ``range`` params
    as ``/alarm-events``.
    """
    db: aiosqlite.Connection = request.app.state.db
    now = time.time()

    if start is not None and end is not None:
        start_ts, end_ts = start, end
    elif range is not None:
        range_s = RANGE_MAP.get(range)
        if range_s is None:
            raise HTTPException(400, f"Unknown range '{range}'")
        start_ts, end_ts = now - range_s, now
    else:
        start_ts, end_ts = now - 1800, now  # default 30m

    raw_alarms = await query_alarm_events(db, start_ts, end_ts, gateway_id=gateway_id)
    alarm_rows = pair_alarm_events(raw_alarms)

    # From transitions in the raw rows, per gateway: bucketed rows report
    # one mode per bucket, which stretched and mislabelled intervals (#78).
    transitions = await query_grid_mode_transitions(
        db, start_ts, end_ts, gateway_id=gateway_id
    )
    by_gw: dict[str, list[dict]] = {}
    for t in transitions:
        by_gw.setdefault(t["gateway_id"], []).append(t)
    clip_rows = [
        row for pts in by_gw.values() for row in compute_grid_mode_intervals(pts)
    ]

    events = sorted(alarm_rows + clip_rows, key=lambda e: e["ts"], reverse=True)
    return {"events": events, "count": len(events)}


@router.get("/settings/metrics")
async def get_metrics_settings(request: Request):
    """Read metrics retention + raw-resolution config."""
    db: aiosqlite.Connection = request.app.state.db
    return {
        "retention_days": await get_retention_days(db),
        "raw_age_days": await get_raw_age_days(db),
    }


class MetricsSettingsUpdate(BaseModel):
    retention_days: int | None = None
    raw_age_days: int | None = None


@router.put("/settings/metrics")
async def put_metrics_settings(body: MetricsSettingsUpdate, request: Request):
    """Update metrics retention and/or the raw full-resolution window.

    raw_age_days is how long full-resolution samples are kept before being
    downsampled to 5-min buckets; it must be >= 1 and <= retention_days
    (keeping raw longer than total retention is meaningless).
    """
    db: aiosqlite.Connection = request.app.state.db
    retention = (
        body.retention_days if body.retention_days is not None
        else await get_retention_days(db)
    )
    if retention < 1:
        raise HTTPException(400, "retention_days must be >= 1")
    if body.raw_age_days is not None and not (1 <= body.raw_age_days <= retention):
        raise HTTPException(
            400, f"raw_age_days must be between 1 and retention_days ({retention})"
        )

    if body.retention_days is not None:
        await set_retention_days(db, body.retention_days)
    if body.raw_age_days is not None:
        await set_raw_age_days(db, body.raw_age_days)

    return {
        "retention_days": await get_retention_days(db),
        "raw_age_days": await get_raw_age_days(db),
    }


@router.get("/stats")
async def get_stats(request: Request):
    """Return operational statistics (uptime, polls, errors, data quality)."""
    stats = getattr(request.app.state, "stats", None)
    if stats is None:
        return {"error": "Stats not initialised"}
    return stats.snapshot.to_dict()


@router.get("/logs")
async def get_logs(
    request: Request,
    limit: int = 2000,
    start_ts: float | None = None,
    end_ts: float | None = None,
    level: str | None = None,
    gateway: str | None = None,
    source: str | None = None,
    search: str | None = None,
):
    """Persisted log history, filterable by time span (start_ts/end_ts) + level/
    gateway/source/search. Returned oldest-first (the UI reverses for display).
    Falls back to the in-memory buffer on a fresh DB (default, no-range view)."""
    db = getattr(request.app.state, "db", None)
    if db is not None:
        result = await query_logs(
            db, start_ts=start_ts, end_ts=end_ts, level=level,
            gateway_id=gateway, source=source, search=search, limit=limit,
        )
        # A given time range (even if empty) is authoritative — only fall back to
        # the buffer for the default view on a not-yet-flushed DB.
        if result["logs"] or start_ts is not None or end_ts is not None:
            return {"logs": list(reversed(result["logs"])), "total": result["total"]}
    log_buffer = getattr(request.app.state, "log_buffer", None)
    if log_buffer is None:
        return {"logs": [], "total": 0}
    entries = list(log_buffer)[-limit:]
    return {"logs": entries, "total": len(log_buffer)}


# ── Sequencer endpoints ──────────────────────────────────────────

def _get_sequences_dir() -> Path:
    """Resolve the sequences directory, preferring the data dir for persistence."""
    from franklinwh_bridge.config.environment import get_data_dir

    bundled_dir = Path(__file__).resolve().parent.parent / "sequences"
    data_dir = get_data_dir()
    seq_dir = data_dir / "sequences"

    if data_dir.is_dir():
        # Use data dir for persistence; seed/update from bundled examples
        seq_dir.mkdir(parents=True, exist_ok=True)
        if bundled_dir.is_dir():
            for src in bundled_dir.glob("*.json"):
                dst = seq_dir / src.name
                # Copy if missing OR if bundled version is newer (code update)
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    dst.write_text(src.read_text())
        return seq_dir

    # Fallback: package-bundled sequences (dev mode)
    return bundled_dir


SEQUENCES_DIR = _get_sequences_dir()


class SequenceExecRequest(BaseModel):
    sequence: list[dict[str, Any]] | None = None
    inline: str | None = None
    dry_run: bool = False
    gateway_id: str | None = None  # None = default, "all" = all active


# Hoisted: Depends() in an argument default trips ruff B008.
_REQUIRE_CONTROL = Depends(require_capability("control"))


def _seq_summary(steps: list[dict[str, Any]]) -> str:
    """Compact "Model.Point=value" list of a sequence's writes, for the audit."""
    parts: list[str] = []
    for st in steps:
        w = st.get("writes") if isinstance(st, dict) else None
        if isinstance(w, dict):
            parts += [f"{k}={v}" for k, v in w.items()]
    return ", ".join(parts)[:400] or f"{len(steps)} step(s)"


@router.post("/sequence/execute")
async def execute_sequence(
    body: SequenceExecRequest, request: Request, _user: dict = _REQUIRE_CONTROL,
):
    """Execute a SunSpec Modbus sequence against one or all gateways.

    It writes real registers (WSetEna/WSetPct included, i.e. a battery
    dispatch), so it needs the ``control`` capability, and every run is
    recorded in the control log with who ran it and what it wrote.
    """
    registry = getattr(request.app.state, "registry", None)

    # Parse the sequence steps
    if body.sequence:
        steps = body.sequence
    elif body.inline:
        try:
            parsed = json.loads(body.inline)
        except json.JSONDecodeError as e:
            return {"ok": False, "output": [f"ERROR: Invalid JSON: {e}"]}

        if isinstance(parsed, dict):
            steps = [{"step": "Inline writes", "writes": parsed, "verify": True}]
        elif isinstance(parsed, list):
            steps = parsed
        else:
            return {"ok": False, "output": ["ERROR: Expected JSON object or array"]}
    else:
        return {"ok": False, "output": ["ERROR: No sequence provided"]}

    # Resolve target gateway(s)
    gw_id = body.gateway_id or "default"

    def _is_mock(inst: object) -> bool:
        return bool(inst is not None and getattr(getattr(inst, "config", None), "mock", False))

    if gw_id == "all" and registry:
        # A sequence reads/writes real Modbus registers; a mock has none (its
        # controller has no .dev and the run errored). Leave mocks out of "all".
        targets = [
            (gid, registry.get(gid))
            for gid in registry.list_active()
            if not _is_mock(registry.get(gid))
        ]
        if not targets:
            return {
                "ok": False,
                "output": [
                    "ERROR: No active real gateways (mock gateways have no Modbus registers)"
                ],
            }
    else:
        # Single gateway
        inst = registry.get(gw_id) if registry else None
        if _is_mock(inst):
            raise HTTPException(
                400,
                "The Sequencer reads and writes real Modbus registers — a mock "
                "gateway has none. Pick a real gateway.",
            )
        if inst and inst.controller:
            targets = [(gw_id, inst)]
        else:
            # Legacy fallback
            ctrl = getattr(request.app.state, "controller", None)
            if ctrl is None:
                raise HTTPException(503, "No Modbus controller available")
            targets = [(gw_id, None)]  # None = use legacy controller

    all_output: list[str] = []
    all_ok = True

    for target_id, inst in targets:
        if len(targets) > 1:
            all_output.append(f"═══ Gateway: {target_id} ═══")

        # Resolve controller + lock for this gateway
        if inst and inst.controller:
            controller = inst.controller
            modbus_lock = inst.modbus_lock
        else:
            controller = getattr(request.app.state, "controller", None)
            modbus_lock = getattr(request.app.state, "modbus_lock", None)

        if controller is None:
            all_output.append(f"ERROR: No controller for {target_id}")
            all_ok = False
            continue

        output_lines: list[str] = []

        class SeqLogHandler(logging.Handler):
            def __init__(self, buf: list[str]) -> None:
                super().__init__()
                self._buf = buf

            def emit(self, record: logging.LogRecord) -> None:
                self._buf.append(self.format(record))

        handler = SeqLogHandler(output_lines)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))

        try:
            from franklinwh_modbus.sequencer import SunSpecSequencer

            seq_logger = logging.getLogger("franklinwh_modbus.sequencer")
            seq_logger.addHandler(handler)
            seq_logger.setLevel(logging.INFO)

            def _run(ctrl=controller, gw=target_id) -> bool:
                dev = ctrl.dev
                seq = SunSpecSequencer(dev)
                seq.verbose = True
                if body.dry_run:
                    return seq.run_sequence(steps, dry_run=True)
                with link_stats.track(gw, "diag", "sequencer") as req:
                    return req.check(seq.run_sequence(steps, dry_run=False))

            who = f"sequencer:user:{_user.get('username') or '?'}"
            with modbus_audit.writing_as(target_id, who):  # #342 audit trail
                if modbus_lock:
                    async with modbus_lock:
                        success = await asyncio.to_thread(_run)
                else:
                    success = await asyncio.to_thread(_run)

            seq_logger.removeHandler(handler)

            if body.dry_run and not all_output:
                output_lines.insert(0, "DRY RUN — no registers written")

            output_lines.append(
                "SUCCESS: Sequence complete"
                if success else "FAIL: Sequence aborted"
            )
            if not success:
                all_ok = False
        except ImportError:
            output_lines.append(
                "ERROR: franklinwh_modbus.sequencer not available"
            )
            all_ok = False
        except Exception as e:
            output_lines.append(f"ERROR: {e}")
            all_ok = False

        all_output.extend(output_lines)

        # Record who wrote what, where and when — the Sequencer can dispatch the
        # battery, and until now it left no record at all.
        ok_here = any(line.startswith("SUCCESS") for line in output_lines)
        who = f"user:{_user.get('username') or '?'}"
        try:
            await log_control_event(
                request.app.state.db,
                event="sequence_dry_run" if body.dry_run else (
                    "sequence_executed" if ok_here else "sequence_failed"),
                action="sequencer",
                detail=f"{_seq_summary(steps)} — by {who}",
                gateway_id=target_id,
            )
        except Exception as exc:
            logger.warning("Failed to record sequencer run: %s", exc)
        logger.info("Sequencer %s on %s by %s: %s",
                    "dry run" if body.dry_run else ("ok" if ok_here else "failed"),
                    target_id, who, _seq_summary(steps))

    return {"ok": all_ok, "output": all_output}


_NAME_RULE = "Names may use letters, digits, _ and - only (up to 64)"


def _check_name(name: str) -> None:
    """A sequence name can't point outside the sequences folder."""
    if not si.valid_name(name):
        raise HTTPException(400, _NAME_RULE)


@router.get("/sequences")
async def list_sequences():
    """List saved sequence files."""
    SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(SEQUENCES_DIR.glob("*.json")):
        try:
            content = f.read_text()
            data = json.loads(content)
            step_count = len(data) if isinstance(data, list) else 1
            files.append({
                "name": f.stem,
                "filename": f.name,
                "steps": step_count,
                "size": f.stat().st_size,
            })
        except Exception:
            files.append({"name": f.stem, "filename": f.name, "steps": 0, "size": 0})
    return {"sequences": files}


@router.get("/sequences/{name}")
async def get_sequence(name: str):
    """Get a saved sequence file's content."""
    _check_name(name)
    path = SEQUENCES_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, f"Sequence '{name}' not found")
    try:
        content = path.read_text()
        data = json.loads(content)
        return {"name": name, "content": data, "raw": content}
    except json.JSONDecodeError:
        return {"name": name, "content": None, "raw": path.read_text()}


class SequenceSaveRequest(BaseModel):
    content: str


@router.put("/sequences/{name}")
async def save_sequence(name: str, body: SequenceSaveRequest):
    """Save or update a sequence file."""
    if not si.valid_name(name):
        return {"ok": False, "error": _NAME_RULE}
    # Validate JSON
    try:
        json.loads(body.content)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Invalid JSON: {e}"}

    SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
    path = SEQUENCES_DIR / f"{name}.json"
    path.write_text(body.content)
    return {"ok": True, "name": name}


@router.delete("/sequences/{name}")
async def delete_sequence(name: str):
    """Delete a sequence file."""
    _check_name(name)
    path = SEQUENCES_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, f"Sequence '{name}' not found")
    path.unlink()
    return {"ok": True, "name": name}


class SequenceRenameRequest(BaseModel):
    new_name: str


@router.post("/sequences/{name}/rename")
async def rename_sequence(name: str, body: SequenceRenameRequest):
    """Rename a sequence file."""
    _check_name(name)
    if not si.valid_name(body.new_name):
        return {"ok": False, "error": _NAME_RULE}
    old_path = SEQUENCES_DIR / f"{name}.json"
    if not old_path.exists():
        raise HTTPException(404, f"Sequence '{name}' not found")
    new_path = SEQUENCES_DIR / f"{body.new_name}.json"
    if new_path.exists():
        return {"ok": False, "error": f"'{body.new_name}' already exists"}
    old_path.rename(new_path)
    return {"ok": True, "old_name": name, "new_name": body.new_name}


# ── Sequence import (#294) ────────────────────────────────────
# Under /sequence-import, not /sequences/..., so no name is shadowed.


class SequencePreviewRequest(BaseModel):
    url: str | None = None
    content: str | None = None


class SequenceImportRequest(BaseModel):
    name: str
    content: str
    source: str = "paste"
    overwrite: bool = False


@router.get("/sequence-import/samples")
async def sequence_samples(_user: dict = _REQUIRE_CONTROL):
    """The latest version's sample sequences on GitHub, marked new, changed or same."""
    try:
        return {"ok": True, **await si.list_samples(SEQUENCES_DIR)}
    except si.SequenceImportError as e:
        return {"ok": False, "error": str(e)}


@router.post("/sequence-import/preview")
async def sequence_preview(body: SequencePreviewRequest, _user: dict = _REQUIRE_CONTROL):
    """Fetch (or take pasted) sequence text and check it. Saves and runs nothing."""
    try:
        if body.url:
            text = await si.fetch(body.url)
        elif body.content:
            text = body.content
        else:
            return {"ok": False, "error": "Give a link or paste a sequence"}
        return {"ok": True, **si.check(text)}
    except si.SequenceImportError as e:
        return {"ok": False, "error": str(e)}


@router.post("/sequence-import/save")
async def sequence_import_save(body: SequenceImportRequest, _user: dict = _REQUIRE_CONTROL):
    """Save a previewed sequence. Asks (``exists``) before replacing one."""
    if not si.valid_name(body.name):
        return {"ok": False, "error": _NAME_RULE}
    try:
        si.check(body.content)
    except si.SequenceImportError as e:
        return {"ok": False, "error": str(e)}
    SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
    path = SEQUENCES_DIR / f"{body.name}.json"
    if path.exists() and not body.overwrite:
        return {"ok": False, "exists": True, "error": f"'{body.name}' already exists"}
    replaced = path.exists()
    path.write_text(body.content)
    logger.info(
        "Sequencer: imported '%s' from %s%s by user:%s",
        body.name, body.source[:200], " (replaced)" if replaced else "",
        _user.get("username") or "?",
    )
    return {"ok": True, "name": body.name, "replaced": replaced}


# ── PICS compliance endpoints ──────────────────────────────────


class PicsUpdateRequest(BaseModel):
    model_id: int
    point_name: str
    status: str
    notes: str | None = None


@router.get("/pics")
async def get_pics(request: Request):
    """Return all PICS compliance statuses."""
    db: aiosqlite.Connection = request.app.state.db
    rows = await get_pics_compliance(db)
    return {"pics": rows}


@router.put("/pics")
async def put_pics(body: PicsUpdateRequest, request: Request):
    """Upsert a PICS compliance status for a model point."""
    db: aiosqlite.Connection = request.app.state.db
    try:
        await set_pics_status(db, body.model_id, body.point_name, body.status, body.notes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "ok": True,
        "model_id": body.model_id,
        "point_name": body.point_name,
        "status": body.status,
    }


# ── Battery limits endpoint ─────────────────────────────────────


@router.get("/battery/limits")
async def get_battery_limits(request: Request):
    """Battery power limits a force command may use: trusted ratings only (#133)."""
    handler = _get_command_handler(request)
    if handler is None:
        return {"max_charge_w": None, "max_discharge_w": None, "source": "unknown"}
    return handler.power_limits


# ── Backup endpoints ───────────────────────────────────────────


def _get_backup_manager(request: Request) -> BackupManager:
    mgr = getattr(request.app.state, "backup_manager", None)
    if mgr is None:
        raise HTTPException(503, "Backup manager not initialised")
    return mgr


class BackupCreateRequest(BaseModel):
    label: str | None = None


@router.post("/backup/create")
async def create_backup(request: Request, body: BackupCreateRequest | None = None):
    """Create a new database backup."""
    mgr = _get_backup_manager(request)
    label = body.label if body else None
    info = await mgr.create(label=label)
    return {
        "name": info.name,
        "created_at": info.created_at,
        "schema_version": info.schema_version,
        "app_version": info.app_version,
        "size_bytes": info.size_bytes,
    }


@router.get("/backup/list")
async def list_backups(request: Request):
    """List available backups, newest first."""
    mgr = _get_backup_manager(request)
    backups = mgr.list_backups()
    return {
        "backups": [
            {
                "name": b.name,
                "created_at": b.created_at,
                "schema_version": b.schema_version,
                "app_version": b.app_version,
                "size_bytes": b.size_bytes,
            }
            for b in backups
        ]
    }


class BackupRestoreRequest(BaseModel):
    name: str


@router.post("/backup/restore")
async def restore_backup(body: BackupRestoreRequest, request: Request):
    """Restore the database from a named backup.

    Creates a pre-restore snapshot automatically.  The bridge should be
    restarted after restore to reload state.
    """
    mgr = _get_backup_manager(request)
    backup_path = mgr._backup_dir / f"{body.name}.zip"
    try:
        await mgr.restore(backup_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Backup '{body.name}' not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "restored_from": body.name}


@router.get("/backup/download/{name}")
async def download_backup(name: str, request: Request):
    """Download a backup archive as a ZIP file."""
    mgr = _get_backup_manager(request)
    backup_path = mgr._backup_dir / f"{name}.zip"
    if not backup_path.exists():
        raise HTTPException(404, f"Backup '{name}' not found")
    return FileResponse(
        path=str(backup_path),
        media_type="application/zip",
        filename=f"{name}.zip",
    )


# ── Metrics export / import ────────────────────────────────────


@router.get("/metrics/export")
async def export_metrics_endpoint(
    request: Request,
    range: str = "24h",  # noqa: A002
    format: str = "csv",  # noqa: A002
):
    """Export metrics as CSV or JSON for the given time range.

    Unlike the chart endpoint, this returns every stored row (no
    downsampling) so exports are lossless.
    """
    if range not in RANGE_MAP:
        raise HTTPException(
            400,
            f"Invalid range '{range}'. Valid: {', '.join(sorted(RANGE_MAP))}",
        )
    if format not in ("csv", "json"):
        raise HTTPException(400, "format must be 'csv' or 'json'")

    db: aiosqlite.Connection = request.app.state.db
    rows = await export_metrics(db, RANGE_MAP[range])

    if format == "json":
        return Response(
            content=json.dumps({"rows": rows, "count": len(rows)}, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="metrics_{range}.json"'
            },
        )

    csv_text = format_metrics_csv(rows)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="metrics_{range}.csv"'
        },
    )


@router.post("/metrics/import")
async def import_metrics_endpoint(request: Request, file: UploadFile):
    """Import metrics from a CSV file.

    Expects columns: timestamp, battery_w, grid_w, solar_w, home_w, soc.
    Timestamp can be ISO-8601 or Unix epoch float.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(400, "File must be a .csv")

    content = await file.read()
    try:
        csv_text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "File must be UTF-8 encoded") from exc

    db: aiosqlite.Connection = request.app.state.db
    imported = await import_metrics_csv(db, csv_text)
    return {"imported_rows": imported}
