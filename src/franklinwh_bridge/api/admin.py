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
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from franklinwh_bridge.modbus.catalog import capture_catalog, load_catalog
from franklinwh_bridge.publish.command_handler import DEFAULT_MAX_POWER_W
from franklinwh_bridge.store.backup import BackupManager
from franklinwh_bridge.store.db import get_pics_compliance, set_pics_status
from franklinwh_bridge.store.metrics import (
    BUCKET_MAP,
    RANGE_MAP,
    archive_old_metrics,
    export_metrics,
    format_metrics_csv,
    get_retention_days,
    get_storage_stats,
    import_metrics_csv,
    query_metrics,
    query_metrics_daterange,
    query_metrics_with_archive,
    set_retention_days,
)

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


@router.get("/gateway")
async def get_gateway(request: Request):
    config = getattr(request.app.state, "config", None)
    if config is None:
        return {"host": "unknown", "port": 502, "unit_id": 1, "poll_interval": 10}
    gw = config.settings.gateway
    return {
        "host": gw.host,
        "port": gw.port,
        "unit_id": gw.unit_id,
        "poll_interval": gw.poll_interval,
    }


@router.get("/gateway/test")
async def test_gateway(request: Request) -> dict:
    """Test TCP connectivity to the configured gateway host:port."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(503, "Bridge configuration not available")
    gw = config.settings.gateway
    host = gw.host
    port = gw.port

    def _tcp_connect() -> float:
        """Blocking TCP connect; returns elapsed seconds."""
        t0 = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
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
async def get_models(request: Request):
    db: aiosqlite.Connection = request.app.state.db
    gateway_id = getattr(request.app.state, "gateway_id", "default")
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


@router.post("/models/{model_id}/read")
async def read_model(model_id: int, request: Request):
    """On-demand read of a single SunSpec model from the device.

    Connects to the controller, calls model.read() to fetch live register
    values, then returns the point name→value map.  Also injects the values
    into the sample bus so the Explorer sees them immediately.
    """
    controller = getattr(request.app.state, "controller", None)
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

    # Acquire Modbus lock to prevent interleaving with poller/commands
    modbus_lock = getattr(request.app.state, "modbus_lock", None)
    if modbus_lock:
        async with modbus_lock:
            result = await asyncio.to_thread(_do_read)
    else:
        result = await asyncio.to_thread(_do_read)
    if "error" in result:
        raise HTTPException(404, result["error"])

    # Inject as sticky points so they survive poll cycles (5-min TTL)
    sample_bus = request.app.state.sample_bus
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
async def refresh_models(request: Request):
    db: aiosqlite.Connection = request.app.state.db
    gateway_id = getattr(request.app.state, "gateway_id", "default")
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
        gw_id = getattr(request.app.state, "gateway_id", "default")
        return {"gateway_id": gw_id, "points": {}, "ts": None}

    # Merge command handler virtual points (software-tracked command state)
    points = dict(last.points)
    command_handler = getattr(request.app.state, "command_handler", None)
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
        points = await query_metrics_daterange(db, start, end, bucket_seconds)
        return {"range": "custom", "start": start, "end": end, "bucket": bucket, "points": points}

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
            db, now - range_s, now, bucket_seconds
        )
    elif range_s > 6 * 3600:
        points = await query_metrics_with_archive(db, range_s)
    else:
        points = await query_metrics(db, range_s)
    return {"range": range_key, "bucket": bucket, "points": points}


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


@router.get("/settings/metrics")
async def get_metrics_settings(request: Request):
    """Read metrics retention config."""
    db: aiosqlite.Connection = request.app.state.db
    retention = await get_retention_days(db)
    return {"retention_days": retention}


class MetricsSettingsUpdate(BaseModel):
    retention_days: int


@router.put("/settings/metrics")
async def put_metrics_settings(body: MetricsSettingsUpdate, request: Request):
    """Update metrics retention config."""
    if body.retention_days < 1:
        raise HTTPException(400, "retention_days must be >= 1")
    db: aiosqlite.Connection = request.app.state.db
    await set_retention_days(db, body.retention_days)
    return {"retention_days": body.retention_days}


@router.get("/stats")
async def get_stats(request: Request):
    """Return operational statistics (uptime, polls, errors, data quality)."""
    stats = getattr(request.app.state, "stats", None)
    if stats is None:
        return {"error": "Stats not initialised"}
    return stats.snapshot.to_dict()


@router.get("/logs")
async def get_logs(request: Request, limit: int = 100):
    log_buffer = getattr(request.app.state, "log_buffer", None)
    if log_buffer is None:
        return {"logs": []}
    entries = list(log_buffer)[-limit:]
    return {"logs": entries}


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


@router.post("/sequence/execute")
async def execute_sequence(body: SequenceExecRequest, request: Request):
    """Execute a SunSpec Modbus sequence against the connected device."""
    controller = getattr(request.app.state, "controller", None)
    if controller is None:
        raise HTTPException(503, "No Modbus controller available")

    # Parse the sequence
    if body.sequence:
        steps = body.sequence
    elif body.inline:
        try:
            parsed = json.loads(body.inline)
        except json.JSONDecodeError as e:
            return {"ok": False, "output": [f"ERROR: Invalid JSON: {e}"]}

        # Inline can be a dict (simple writes) or a list (full sequence)
        if isinstance(parsed, dict):
            steps = [{"step": "Inline writes", "writes": parsed, "verify": True}]
        elif isinstance(parsed, list):
            steps = parsed
        else:
            return {"ok": False, "output": ["ERROR: Expected JSON object or array"]}
    else:
        return {"ok": False, "output": ["ERROR: No sequence provided"]}

    # Capture log output from the sequencer
    output_lines: list[str] = []

    class SeqLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = self.format(record)
            output_lines.append(msg)

    handler = SeqLogHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    try:
        from franklinwh_modbus.sequencer import SunSpecSequencer

        seq_logger = logging.getLogger("franklinwh_modbus.sequencer")
        seq_logger.addHandler(handler)
        seq_logger.setLevel(logging.INFO)

        def _run() -> bool:
            dev = controller.dev
            seq = SunSpecSequencer(dev)
            seq.verbose = True
            return seq.run_sequence(steps, dry_run=body.dry_run)

        # Acquire Modbus lock to prevent interleaving with poller/commands
        modbus_lock = getattr(request.app.state, "modbus_lock", None)
        if modbus_lock:
            async with modbus_lock:
                success = await asyncio.to_thread(_run)
        else:
            success = await asyncio.to_thread(_run)

        seq_logger.removeHandler(handler)

        if body.dry_run:
            output_lines.insert(0, "DRY RUN — no registers written")

        output_lines.append(
            "SUCCESS: Sequence complete" if success else "FAIL: Sequence aborted"
        )
        return {"ok": success, "output": output_lines}
    except ImportError:
        return {
            "ok": False,
            "output": ["ERROR: franklinwh_modbus.sequencer not available"],
        }
    except Exception as e:
        output_lines.append(f"ERROR: {e}")
        return {"ok": False, "output": output_lines}


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
    old_path = SEQUENCES_DIR / f"{name}.json"
    if not old_path.exists():
        raise HTTPException(404, f"Sequence '{name}' not found")
    new_path = SEQUENCES_DIR / f"{body.new_name}.json"
    if new_path.exists():
        return {"ok": False, "error": f"'{body.new_name}' already exists"}
    old_path.rename(new_path)
    return {"ok": True, "old_name": name, "new_name": body.new_name}


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
    """Return current battery power limits (from M702 nameplate or defaults)."""
    handler = _get_command_handler(request)
    if handler is None:
        return {
            "max_charge_w": DEFAULT_MAX_POWER_W,
            "max_discharge_w": DEFAULT_MAX_POWER_W,
            "source": "default",
        }
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
