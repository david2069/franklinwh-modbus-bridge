"""SunSpec model catalog — capture, persist, hash, and diff."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


def extract_catalog_from_controller(controller: Any) -> dict:
    """Extract SunSpec model catalog from a connected controller.

    Walks the pysunspec2 model objects in ``controller.models`` and builds a
    ``device_info`` dict identical to the format produced by the reader tool.
    Only reads in-memory model definitions — no Modbus register I/O.
    """
    device_info: dict[str, Any] = {"models": {}}

    for model_id, model_val in controller.models.items():
        if not isinstance(model_id, int):
            continue

        # Handle list-wrapped models (pysunspec2 can return [model] or model)
        model_obj = model_val[0] if isinstance(model_val, list) and model_val else model_val
        if model_obj is None:
            continue

        # Model name/label from gdef (group definition) or model_type
        model_name = f"Model {model_id}"
        gdef = getattr(model_obj, "gdef", None)
        if gdef:
            model_name = (
                gdef.get("label") or gdef.get("name") or model_name
            )
        elif hasattr(model_obj, "model_type"):
            mt = model_obj.model_type
            if hasattr(mt, "label"):
                model_name = mt.label or model_name

        # Model base address for computing absolute point addresses
        # pysunspec2 uses `model_addr` for the Modbus register base
        model_addr = getattr(model_obj, "model_addr", None)
        if model_addr is None:
            model_addr = getattr(model_obj, "addr", None)
        if model_addr is None:
            model_addr = getattr(model_obj, "offset", None)

        # Extract points
        points: list[dict[str, Any]] = []
        point_names = getattr(model_obj, "points", None)
        if point_names:
            for pt_name in point_names:
                pt = getattr(model_obj, pt_name, None)
                if pt is None:
                    continue

                point_info: dict[str, Any] = {"name": pt_name}

                # Point definition metadata
                pdef = getattr(pt, "pdef", None)
                if pdef is None:
                    pdef = getattr(pt, "point_type", None)

                if pdef:
                    _pdef = pdef  # bind for lambda closure

                    _get = (
                        _pdef.get
                        if isinstance(_pdef, dict)
                        else lambda k, d=None, _p=_pdef: getattr(_p, k, d)
                    )

                    for key in ("type", "label", "desc", "access"):
                        v = _get(key, None)
                        if v is not None:
                            point_info[key] = str(v) if key == "type" else v

                    # Units — pysunspec2 uses 'units', fixture uses 'units'
                    units = _get("units", None)
                    if units:
                        point_info["units"] = units

                    # Scale factor reference
                    sf = _get("sf", None)
                    if sf:
                        point_info["scale_factor"] = sf

                    # Register size
                    size = _get("size", None)
                    if size:
                        point_info["size"] = size

                    # Compute absolute address from model base + point offset
                    # pysunspec2 stores offset on the point object, not in pdef
                    pt_offset = getattr(pt, "offset", None)
                    if pt_offset is None:
                        pt_offset = _get("offset", None)
                    if pt_offset is not None and model_addr is not None:
                        point_info["address"] = model_addr + pt_offset
                    elif hasattr(pt, "addr") and pt.addr is not None:
                        point_info["address"] = pt.addr

                    # Symbols (enum/bitmap values)
                    symbols_raw = _get("symbols", None)
                    if symbols_raw:
                        sym_dict: dict[str, str] = {}
                        try:
                            if isinstance(symbols_raw, (list, tuple)):
                                for sym in symbols_raw:
                                    if isinstance(sym, dict):
                                        sym_dict[str(sym["value"])] = (
                                            sym.get("label") or sym.get("name", "")
                                        )
                                    elif hasattr(sym, "value"):
                                        sym_dict[str(sym.value)] = (
                                            getattr(sym, "label", None)
                                            or getattr(sym, "name", "")
                                        )
                            elif isinstance(symbols_raw, dict):
                                sym_dict = {
                                    str(k): str(v) for k, v in symbols_raw.items()
                                }
                        except (TypeError, KeyError, AttributeError):
                            pass
                        if sym_dict:
                            point_info["symbols"] = sym_dict

                points.append(point_info)

        device_info["models"][str(model_id)] = {
            "id": model_id,
            "name": model_name,
            "points": points,
        }

    logger.info(
        "Extracted catalog from controller: %d models, %d total points",
        len(device_info["models"]),
        sum(len(m["points"]) for m in device_info["models"].values()),
    )
    return device_info


@dataclass
class CatalogDiff:
    added_models: list[int]
    removed_models: list[int]
    added_points: list[str]
    removed_points: list[str]
    changed_points: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added_models
            or self.removed_models
            or self.added_points
            or self.removed_points
            or self.changed_points
        )


def _hash_catalog(device_info: dict) -> str:
    """Deterministic hash of the models/points structure."""
    models = device_info.get("models", {})
    canonical = json.dumps(models, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def parse_device_info(device_info: dict) -> list[dict]:
    """Parse the reader's JSON output into a flat list of model+point records.

    Accepts both the bridge's simplified format (``writable`` bool) and the
    reader tool's full format (``access`` string, ``label``, ``scale_factor``,
    ``symbols``, etc.).
    """
    records: list[dict] = []
    models = device_info.get("models", {})

    for model_key, model_data in models.items():
        model_id = model_data.get("id", int(model_key))
        model_label = model_data.get("name", f"model_{model_id}")

        for point in model_data.get("points", []):
            access = point.get("access")
            if access is None:
                access = "RW" if point.get("writable") else "R"

            symbols = point.get("symbols")
            symbols_json = json.dumps(symbols) if symbols else None

            records.append({
                "model_id": model_id,
                "model_label": model_label,
                "point_name": point.get("name", ""),
                "type": point.get("type"),
                "unit": point.get("unit") or point.get("units"),
                "address": point.get("address"),
                "writable": "W" in (access or ""),
                "label": point.get("label"),
                "description": point.get("desc"),
                "scale_factor": point.get("scale_factor"),
                "symbols_json": symbols_json,
                "access": access,
                "size": point.get("size"),
            })

    return records


async def capture_catalog(
    device_info: dict,
    db: aiosqlite.Connection,
    gateway_id: str,
) -> tuple[str, CatalogDiff]:
    """Persist a captured device catalog and return the hash + diff vs. previous."""
    new_hash = _hash_catalog(device_info)
    now = time.time()
    records = parse_device_info(device_info)

    old_snapshot = await _load_snapshot(db, gateway_id)
    diff = _compute_diff(old_snapshot, records)

    await _clear_catalog(db, gateway_id)

    model_ids_seen: dict[int, int] = {}
    for rec in records:
        mid = rec["model_id"]
        if mid not in model_ids_seen:
            cursor = await db.execute(
                "INSERT INTO device_models (gateway_id, model_id, label, captured_at, hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (gateway_id, mid, rec["model_label"], now, new_hash),
            )
            model_ids_seen[mid] = cursor.lastrowid  # type: ignore[assignment]

        model_db_id = model_ids_seen[mid]
        await db.execute(
            "INSERT INTO device_points "
            "(model_db_id, point_name, type, unit, addr, writable, "
            " label, description, scale_factor, symbols_json, access, size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model_db_id,
                rec["point_name"],
                rec["type"],
                rec["unit"],
                rec["address"],
                int(rec["writable"]),
                rec.get("label"),
                rec.get("description"),
                rec.get("scale_factor"),
                rec.get("symbols_json"),
                rec.get("access", "R"),
                rec.get("size"),
            ),
        )

    await db.commit()

    if diff.has_changes:
        logger.info(
            "Catalog updated: +%d/-%d models, +%d/-%d/~%d points",
            len(diff.added_models),
            len(diff.removed_models),
            len(diff.added_points),
            len(diff.removed_points),
            len(diff.changed_points),
        )
    else:
        logger.info("Catalog unchanged (hash=%s)", new_hash)

    return new_hash, diff


async def load_catalog(db: aiosqlite.Connection, gateway_id: str) -> list[dict]:
    """Load the current catalog from the DB as a list of point records."""
    query = """
        SELECT dm.model_id, dm.label,
               dp.point_name, dp.type, dp.unit, dp.addr, dp.writable,
               dp.label, dp.description, dp.scale_factor,
               dp.symbols_json, dp.access, dp.size
        FROM device_points dp
        JOIN device_models dm ON dp.model_db_id = dm.id
        WHERE dm.gateway_id = ?
        ORDER BY dm.model_id, dp.point_name
    """
    rows: list[dict] = []
    async with db.execute(query, (gateway_id,)) as cursor:
        async for row in cursor:
            symbols = None
            if row[10]:
                symbols = json.loads(row[10])
            rows.append({
                "model_id": row[0],
                "model_label": row[1],
                "point_name": row[2],
                "type": row[3],
                "unit": row[4],
                "address": row[5],
                "writable": bool(row[6]),
                "label": row[7],
                "description": row[8],
                "scale_factor": row[9],
                "symbols": symbols,
                "access": row[11],
                "size": row[12],
            })
    return rows


async def _load_snapshot(db: aiosqlite.Connection, gateway_id: str) -> list[dict]:
    """Load existing catalog for diffing."""
    return await load_catalog(db, gateway_id)


async def _clear_catalog(db: aiosqlite.Connection, gateway_id: str) -> None:
    """Remove old catalog entries for a gateway."""
    async with db.execute(
        "SELECT id FROM device_models WHERE gateway_id = ?", (gateway_id,)
    ) as cursor:
        model_ids = [row[0] async for row in cursor]

    for mid in model_ids:
        await db.execute("DELETE FROM device_points WHERE model_db_id = ?", (mid,))
    await db.execute("DELETE FROM device_models WHERE gateway_id = ?", (gateway_id,))


def _point_key(rec: dict) -> str:
    return f"{rec['model_id']}.{rec['point_name']}"


def _point_signature(rec: dict) -> str:
    return (
        f"{rec['type']}|{rec['unit']}|{rec['address']}|{rec['writable']}"
        f"|{rec.get('access', '')}|{rec.get('scale_factor', '')}"
    )


def _compute_diff(old: list[dict], new: list[dict]) -> CatalogDiff:
    """Compute the diff between two catalog snapshots."""
    old_models = {r["model_id"] for r in old}
    new_models = {r["model_id"] for r in new}

    old_points = {_point_key(r): _point_signature(r) for r in old}
    new_points = {_point_key(r): _point_signature(r) for r in new}

    old_keys = set(old_points.keys())
    new_keys = set(new_points.keys())

    changed = [
        k for k in old_keys & new_keys if old_points[k] != new_points[k]
    ]

    return CatalogDiff(
        added_models=sorted(new_models - old_models),
        removed_models=sorted(old_models - new_models),
        added_points=sorted(new_keys - old_keys),
        removed_points=sorted(old_keys - new_keys),
        changed_points=sorted(changed),
    )
