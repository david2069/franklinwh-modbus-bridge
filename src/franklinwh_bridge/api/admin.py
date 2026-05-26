"""Admin REST routes — the internal gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from franklinwh_bridge.modbus.catalog import capture_catalog, load_catalog
from franklinwh_bridge.store.metrics import (
    RANGE_MAP,
    get_retention_days,
    query_metrics,
    set_retention_days,
)

logger = logging.getLogger(__name__)

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
            "label": rec.get("label"),
            "description": rec.get("description"),
            "access": rec.get("access", "R"),
            "scale_factor": rec.get("scale_factor"),
            "size": rec.get("size"),
            "symbols": rec.get("symbols"),
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


@router.get("/metrics")
async def get_metrics(request: Request, range: str = "30m"):  # noqa: A002
    """Return power time-series for the dashboard chart."""
    if range not in RANGE_MAP:
        raise HTTPException(
            400,
            f"Invalid range '{range}'. Valid: {', '.join(sorted(RANGE_MAP))}",
        )
    db: aiosqlite.Connection = request.app.state.db
    points = await query_metrics(db, RANGE_MAP[range])
    return {"range": range, "points": points}


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
        # Use data dir for persistence; seed with bundled examples
        seq_dir.mkdir(parents=True, exist_ok=True)
        if bundled_dir.is_dir():
            for src in bundled_dir.glob("*.json"):
                dst = seq_dir / src.name
                if not dst.exists():
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
