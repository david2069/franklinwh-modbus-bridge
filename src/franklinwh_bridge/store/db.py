"""SQLite config store with WAL mode and forward-only migrations."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS gateways (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL DEFAULT 502,
        unit_id INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS gateway_state (
        gateway_id TEXT PRIMARY KEY REFERENCES gateways(id),
        conn_state TEXT NOT NULL DEFAULT 'disconnected',
        last_ok_ts REAL,
        last_error TEXT
    );

    CREATE TABLE IF NOT EXISTS device_models (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gateway_id TEXT NOT NULL REFERENCES gateways(id),
        model_id INTEGER NOT NULL,
        label TEXT,
        captured_at REAL NOT NULL,
        hash TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS device_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_db_id INTEGER NOT NULL REFERENCES device_models(id),
        point_name TEXT NOT NULL,
        type TEXT,
        unit TEXT,
        addr INTEGER,
        writable INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS entity_map (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        point_id INTEGER NOT NULL REFERENCES device_points(id),
        ha_component TEXT NOT NULL,
        ha_config_json TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS mqtt_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        host TEXT NOT NULL DEFAULT 'localhost',
        port INTEGER NOT NULL DEFAULT 1883,
        username TEXT,
        password TEXT,
        tls_mode TEXT NOT NULL DEFAULT 'off'
    );

    CREATE TABLE IF NOT EXISTS app_config (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS metric_samples (
        gateway_id TEXT NOT NULL,
        point_id TEXT NOT NULL,
        ts REAL NOT NULL,
        value REAL,
        quality TEXT NOT NULL DEFAULT 'ok'
    );

    CREATE INDEX IF NOT EXISTS idx_metric_samples_ts
        ON metric_samples(gateway_id, point_id, ts);

    CREATE TABLE IF NOT EXISTS startup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        event TEXT NOT NULL,
        detail TEXT
    );
    """,
}


async def get_schema_version(db: aiosqlite.Connection) -> int:
    """Get the current schema version, or 0 if no schema exists."""
    try:
        async with db.execute(
            "SELECT MAX(version) FROM schema_version"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0
    except aiosqlite.OperationalError:
        return 0


async def run_migrations(db: aiosqlite.Connection) -> int:
    """Apply pending migrations. Returns the final schema version."""
    current = await get_schema_version(db)

    for version in sorted(MIGRATIONS.keys()):
        if version <= current:
            continue
        logger.info("Applying migration v%d", version)
        await db.executescript(MIGRATIONS[version])
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
        await db.commit()

    final = await get_schema_version(db)
    logger.info("Schema at v%d", final)
    return final


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """Open the database, enable WAL + foreign keys, and run migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await run_migrations(db)
    return db


async def log_startup_event(db: aiosqlite.Connection, event: str, detail: str | None = None):
    """Write a startup log entry."""
    await db.execute(
        "INSERT INTO startup_log (ts, event, detail) VALUES (?, ?, ?)",
        (time.time(), event, detail),
    )
    await db.commit()
