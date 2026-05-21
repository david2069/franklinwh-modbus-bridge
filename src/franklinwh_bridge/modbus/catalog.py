"""SunSpec model catalog — capture, persist, hash, and diff."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


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
    """Parse the reader's JSON output into a flat list of model+point records."""
    records: list[dict] = []
    models = device_info.get("models", {})

    for model_key, model_data in models.items():
        model_id = model_data.get("id", int(model_key))
        model_label = model_data.get("name", f"model_{model_id}")

        for point in model_data.get("points", []):
            records.append({
                "model_id": model_id,
                "model_label": model_label,
                "point_name": point.get("name", ""),
                "type": point.get("type"),
                "unit": point.get("unit"),
                "address": point.get("address"),
                "writable": bool(point.get("writable", False)),
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
            "(model_db_id, point_name, type, unit, addr, writable) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                model_db_id,
                rec["point_name"],
                rec["type"],
                rec["unit"],
                rec["address"],
                int(rec["writable"]),
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
        SELECT dm.model_id, dm.label, dp.point_name, dp.type, dp.unit, dp.addr, dp.writable
        FROM device_points dp
        JOIN device_models dm ON dp.model_db_id = dm.id
        WHERE dm.gateway_id = ?
        ORDER BY dm.model_id, dp.point_name
    """
    rows: list[dict] = []
    async with db.execute(query, (gateway_id,)) as cursor:
        async for row in cursor:
            rows.append({
                "model_id": row[0],
                "model_label": row[1],
                "point_name": row[2],
                "type": row[3],
                "unit": row[4],
                "address": row[5],
                "writable": bool(row[6]),
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
    return f"{rec['type']}|{rec['unit']}|{rec['address']}|{rec['writable']}"


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
