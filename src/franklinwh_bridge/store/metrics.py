"""Metrics sink/reader — SQLite backend for v1."""

from __future__ import annotations

import logging
import time
from typing import Protocol

import aiosqlite

from franklinwh_bridge.modbus.sample import Sample

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 14


class MetricsSink(Protocol):
    async def write_sample(self, sample: Sample) -> None: ...
    async def prune(self, ttl_days: int = DEFAULT_TTL_DAYS) -> int: ...


class MetricsReader(Protocol):
    async def read_latest(self, gateway_id: str) -> dict[str, float | int | str | None]: ...

    async def read_history(
        self,
        gateway_id: str,
        point_id: str,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 1000,
    ) -> list[tuple[float, float | None, str]]: ...


class SQLiteMetrics:
    """SQLite-backed metrics sink and reader."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def write_sample(self, sample: Sample) -> None:
        rows = [
            (sample.gateway_id, point_id, sample.ts, value, sample.quality)
            for point_id, value in sample.points.items()
            if isinstance(value, (int, float))
        ]
        if rows:
            await self._db.executemany(
                "INSERT INTO metric_samples (gateway_id, point_id, ts, value, quality) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await self._db.commit()

    async def prune(self, ttl_days: int = DEFAULT_TTL_DAYS) -> int:
        cutoff = time.time() - (ttl_days * 86400)
        cursor = await self._db.execute(
            "DELETE FROM metric_samples WHERE ts < ?", (cutoff,)
        )
        await self._db.commit()
        pruned = cursor.rowcount
        if pruned:
            logger.info("Pruned %d metric rows older than %d days", pruned, ttl_days)
        return pruned

    async def read_latest(self, gateway_id: str) -> dict[str, float | int | str | None]:
        query = """
            SELECT point_id, value
            FROM metric_samples
            WHERE gateway_id = ?
            AND ts = (SELECT MAX(ts) FROM metric_samples WHERE gateway_id = ?)
        """
        result: dict[str, float | int | str | None] = {}
        async with self._db.execute(query, (gateway_id, gateway_id)) as cursor:
            async for row in cursor:
                result[row[0]] = row[1]
        return result

    async def read_history(
        self,
        gateway_id: str,
        point_id: str,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 1000,
    ) -> list[tuple[float, float | None, str]]:
        conditions = ["gateway_id = ?", "point_id = ?"]
        params: list = [gateway_id, point_id]
        if from_ts is not None:
            conditions.append("ts >= ?")
            params.append(from_ts)
        if to_ts is not None:
            conditions.append("ts <= ?")
            params.append(to_ts)

        query = (
            f"SELECT ts, value, quality FROM metric_samples "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY ts DESC LIMIT ?"
        )
        params.append(limit)

        rows: list[tuple[float, float | None, str]] = []
        async with self._db.execute(query, params) as cursor:
            async for row in cursor:
                rows.append((row[0], row[1], row[2]))
        return rows
