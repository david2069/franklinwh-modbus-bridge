"""SQLite config store with WAL mode and forward-only migrations."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 5

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
    2: """
    ALTER TABLE device_points ADD COLUMN label TEXT;
    ALTER TABLE device_points ADD COLUMN description TEXT;
    ALTER TABLE device_points ADD COLUMN scale_factor TEXT;
    ALTER TABLE device_points ADD COLUMN symbols_json TEXT;
    ALTER TABLE device_points ADD COLUMN access TEXT NOT NULL DEFAULT 'R';
    ALTER TABLE device_points ADD COLUMN size INTEGER;
    """,
    3: """
    ALTER TABLE mqtt_config ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE mqtt_config ADD COLUMN client_id TEXT NOT NULL DEFAULT 'franklinwh_bridge';
    ALTER TABLE mqtt_config ADD COLUMN qos INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE mqtt_config ADD COLUMN retain_discovery INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE mqtt_config ADD COLUMN topic_prefix TEXT NOT NULL DEFAULT 'franklinwh';
    ALTER TABLE mqtt_config ADD COLUMN discovery_prefix TEXT NOT NULL DEFAULT 'homeassistant';
    INSERT OR IGNORE INTO mqtt_config (id) VALUES (1);
    """,
    4: """
    CREATE TABLE IF NOT EXISTS control_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        event TEXT NOT NULL,
        action TEXT,
        power_w INTEGER,
        detail TEXT,
        hw_state_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_control_log_ts ON control_log(ts);

    CREATE TABLE IF NOT EXISTS control_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        active INTEGER NOT NULL DEFAULT 0,
        action TEXT NOT NULL DEFAULT '',
        power_w INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL DEFAULT 0,
        watchdog_s INTEGER NOT NULL DEFAULT 3600,
        updated_at REAL NOT NULL DEFAULT 0
    );
    INSERT OR IGNORE INTO control_state (id) VALUES (1);
    """,
    5: """
    CREATE TABLE IF NOT EXISTS metrics (
        ts REAL NOT NULL,
        battery_w REAL,
        grid_w REAL,
        solar_w REAL,
        home_w REAL,
        soc REAL
    );
    CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
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


async def log_control_event(
    db: aiosqlite.Connection,
    event: str,
    action: str = "",
    power_w: int = 0,
    detail: str = "",
    hw_state: dict | None = None,
) -> None:
    import json
    hw_json = json.dumps(hw_state) if hw_state else None
    await db.execute(
        "INSERT INTO control_log (ts, event, action, power_w, detail, hw_state_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), event, action, power_w, detail, hw_json),
    )
    await db.commit()


async def save_control_state(
    db: aiosqlite.Connection,
    active: bool,
    action: str = "",
    power_w: int = 0,
    started_at: float = 0,
    watchdog_s: int = 3600,
) -> None:
    await db.execute(
        "UPDATE control_state SET active=?, action=?, power_w=?, "
        "started_at=?, watchdog_s=?, updated_at=? WHERE id=1",
        (int(active), action, power_w, started_at, watchdog_s, time.time()),
    )
    await db.commit()


async def load_control_state(db: aiosqlite.Connection) -> dict:
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM control_state WHERE id=1") as cur:
            row = await cur.fetchone()
    finally:
        db.row_factory = None
    if row is None:
        return {"active": False, "action": "", "power_w": 0, "started_at": 0, "watchdog_s": 3600}
    return {
        "active": bool(row["active"]),
        "action": row["action"],
        "power_w": row["power_w"],
        "started_at": row["started_at"],
        "watchdog_s": row["watchdog_s"],
        "updated_at": row["updated_at"],
    }


MQTT_CONFIG_COLUMNS = (
    "host", "port", "username", "password", "tls_mode",
    "enabled", "client_id", "qos", "retain_discovery",
    "topic_prefix", "discovery_prefix",
)

MQTT_CONFIG_DEFAULTS = {
    "host": "localhost",
    "port": 1883,
    "username": None,
    "password": None,
    "tls_mode": "off",
    "enabled": True,
    "client_id": "franklinwh_bridge",
    "qos": 0,
    "retain_discovery": True,
    "topic_prefix": "franklinwh",
    "discovery_prefix": "homeassistant",
}


async def get_mqtt_config(db: aiosqlite.Connection) -> dict:
    """Read the singleton MQTT config row, returning defaults if absent."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM mqtt_config WHERE id = 1") as cursor:
            row = await cursor.fetchone()
    finally:
        db.row_factory = None

    if row is None:
        return dict(MQTT_CONFIG_DEFAULTS)

    result = {}
    for col in MQTT_CONFIG_COLUMNS:
        val = row[col]
        if col in ("enabled", "retain_discovery"):
            val = bool(val)
        result[col] = val
    return result


async def set_mqtt_config(db: aiosqlite.Connection, updates: dict) -> dict:
    """Update MQTT config fields. Returns the full config after update."""
    allowed = set(MQTT_CONFIG_COLUMNS)
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return await get_mqtt_config(db)

    for col in ("enabled", "retain_discovery"):
        if col in filtered:
            filtered[col] = int(bool(filtered[col]))

    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values())
    await db.execute(
        f"UPDATE mqtt_config SET {set_clause} WHERE id = 1",  # noqa: S608
        values,
    )
    await db.commit()
    return await get_mqtt_config(db)
