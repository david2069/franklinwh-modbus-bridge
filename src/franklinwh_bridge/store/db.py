"""SQLite config store with WAL mode and forward-only migrations."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 8

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
    6: """
    CREATE TABLE IF NOT EXISTS pics_compliance (
        model_id    INTEGER NOT NULL,
        point_name  TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'U',
        notes       TEXT,
        updated_at  REAL NOT NULL,
        PRIMARY KEY (model_id, point_name)
    );
    """,
    7: """
    CREATE TABLE IF NOT EXISTS operational_stats (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        started_at  REAL NOT NULL,
        polls_ok    INTEGER NOT NULL DEFAULT 0,
        polls_stale INTEGER NOT NULL DEFAULT 0,
        polls_error INTEGER NOT NULL DEFAULT 0,
        samples_recorded INTEGER NOT NULL DEFAULT 0,
        samples_rejected INTEGER NOT NULL DEFAULT 0,
        sanitizations    INTEGER NOT NULL DEFAULT 0,
        conn_drops       INTEGER NOT NULL DEFAULT 0,
        conn_recoveries  INTEGER NOT NULL DEFAULT 0,
        mqtt_sent        INTEGER NOT NULL DEFAULT 0,
        last_poll_ts     REAL,
        last_error       TEXT,
        updated_at       REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS metrics_archive (
        ts           REAL NOT NULL,
        battery_w    REAL,
        grid_w       REAL,
        solar_w      REAL,
        home_w       REAL,
        soc          REAL,
        sample_count INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_metrics_archive_ts
        ON metrics_archive(ts);
    """,
    # v8 is applied via Python code in run_migrations (needs entity import)
    8: """
    CREATE TABLE IF NOT EXISTS publishing_groups (
        slug        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        enabled     INTEGER NOT NULL DEFAULT 1,
        is_default  INTEGER NOT NULL DEFAULT 1,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS publishing_group_members (
        group_slug   TEXT NOT NULL REFERENCES publishing_groups(slug) ON DELETE CASCADE,
        entity_slug  TEXT NOT NULL,
        PRIMARY KEY (group_slug, entity_slug)
    );

    CREATE INDEX IF NOT EXISTS idx_pgm_entity
        ON publishing_group_members(entity_slug);
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

        # v8: seed default publishing groups from entity definitions
        if version == 8:
            await seed_default_groups(db)

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


# ── PICS compliance ──────────────────────────────────────────

PICS_VALID_STATUSES = frozenset("STFUX")


async def get_pics_compliance(db: aiosqlite.Connection) -> list[dict]:
    """Return all PICS compliance rows."""
    db.row_factory = aiosqlite.Row
    try:
        rows = []
        async with db.execute(
            "SELECT model_id, point_name, status, notes, updated_at "
            "FROM pics_compliance ORDER BY model_id, point_name"
        ) as cursor:
            async for row in cursor:
                rows.append({
                    "model_id": row["model_id"],
                    "point_name": row["point_name"],
                    "status": row["status"],
                    "notes": row["notes"],
                    "updated_at": row["updated_at"],
                })
        return rows
    finally:
        db.row_factory = None


async def set_pics_status(
    db: aiosqlite.Connection,
    model_id: int,
    point_name: str,
    status: str,
    notes: str | None = None,
) -> None:
    """Upsert a PICS compliance status for a model point."""
    if status not in PICS_VALID_STATUSES:
        raise ValueError(f"Invalid PICS status '{status}', must be one of {PICS_VALID_STATUSES}")
    await db.execute(
        "INSERT INTO pics_compliance (model_id, point_name, status, notes, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(model_id, point_name) DO UPDATE SET "
        "status=excluded.status, notes=excluded.notes, "
        "updated_at=excluded.updated_at",
        (model_id, point_name, status, notes, time.time()),
    )
    await db.commit()


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


# ── Publishing Groups ──────────────────────────────────────────

DEFAULT_GROUPS: list[dict] = [
    {
        "slug": "battery",
        "name": "Battery",
        "description": "SOC, SOH, power, and charge state",
        "sort_order": 0,
    },
    {
        "slug": "capacity",
        "name": "Battery Capacity",
        "description": "Total/available capacity and max charge/discharge rates",
        "sort_order": 1,
    },
    {
        "slug": "power",
        "name": "Power",
        "description": "Grid, home load, and per-phase active power",
        "sort_order": 2,
    },
    {
        "slug": "energy",
        "name": "Energy",
        "description": "Grid import/export, solar, and battery energy counters",
        "sort_order": 3,
    },
    {
        "slug": "solar",
        "name": "Solar",
        "description": "Solar power and PV string breakdown",
        "sort_order": 4,
    },
    {
        "slug": "status",
        "name": "Status & Diagnostics",
        "description": "Voltages, frequency, temperatures, control status, timers",
        "sort_order": 5,
    },
    {
        "slug": "control",
        "name": "Control",
        "description": "Writable entities: operating mode, reserves, battery commands",
        "sort_order": 6,
    },
]


async def seed_default_groups(db: aiosqlite.Connection) -> None:
    """Populate default publishing groups from entity state_group values.

    Called once after migration v8 DDL.  Imports entity definitions lazily
    to avoid circular import at module level.
    """
    from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES

    now = time.time()

    for grp in DEFAULT_GROUPS:
        await db.execute(
            "INSERT OR IGNORE INTO publishing_groups "
            "(slug, name, description, enabled, is_default, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 1, ?, ?, ?)",
            (grp["slug"], grp["name"], grp["description"], grp["sort_order"], now, now),
        )

    # Map each entity to its state_group
    for ent in BRIDGE_ENTITIES:
        await db.execute(
            "INSERT OR IGNORE INTO publishing_group_members (group_slug, entity_slug) "
            "VALUES (?, ?)",
            (ent.state_group, ent.slug),
        )

    await db.commit()
    logger.info("Seeded %d default publishing groups", len(DEFAULT_GROUPS))


async def get_publishing_groups(db: aiosqlite.Connection) -> list[dict]:
    """List all publishing groups with member counts."""
    db.row_factory = aiosqlite.Row
    try:
        rows = []
        async with db.execute(
            "SELECT g.*, "
            "(SELECT COUNT(*) FROM publishing_group_members m "
            " WHERE m.group_slug = g.slug) AS member_count "
            "FROM publishing_groups g ORDER BY g.sort_order, g.slug"
        ) as cursor:
            async for row in cursor:
                rows.append({
                    "slug": row["slug"],
                    "name": row["name"],
                    "description": row["description"],
                    "enabled": bool(row["enabled"]),
                    "is_default": bool(row["is_default"]),
                    "sort_order": row["sort_order"],
                    "member_count": row["member_count"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })
        return rows
    finally:
        db.row_factory = None


async def get_publishing_group(db: aiosqlite.Connection, slug: str) -> dict | None:
    """Get a single publishing group with its member slugs."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT * FROM publishing_groups WHERE slug = ?", (slug,)
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        db.row_factory = None

    if row is None:
        return None

    members: list[str] = []
    async with db.execute(
        "SELECT entity_slug FROM publishing_group_members "
        "WHERE group_slug = ? ORDER BY entity_slug",
        (slug,),
    ) as cursor:
        async for mrow in cursor:
            members.append(mrow[0])

    return {
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "is_default": bool(row["is_default"]),
        "sort_order": row["sort_order"],
        "members": members,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def create_publishing_group(
    db: aiosqlite.Connection,
    slug: str,
    name: str,
    description: str = "",
    enabled: bool = True,
    members: list[str] | None = None,
) -> dict:
    """Create a new (non-default) publishing group."""
    now = time.time()
    # Find next sort_order
    async with db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM publishing_groups"
    ) as cursor:
        row = await cursor.fetchone()
        sort_order = row[0] if row else 0

    await db.execute(
        "INSERT INTO publishing_groups "
        "(slug, name, description, enabled, is_default, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (slug, name, description, int(enabled), sort_order, now, now),
    )
    if members:
        await db.executemany(
            "INSERT OR IGNORE INTO publishing_group_members (group_slug, entity_slug) "
            "VALUES (?, ?)",
            [(slug, m) for m in members],
        )
    await db.commit()
    return await get_publishing_group(db, slug)  # type: ignore[return-value]


async def update_publishing_group(
    db: aiosqlite.Connection,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> dict | None:
    """Update mutable fields of a publishing group."""
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if enabled is not None:
        updates["enabled"] = int(enabled)
    if not updates:
        return await get_publishing_group(db, slug)

    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())
    values.append(slug)
    await db.execute(
        f"UPDATE publishing_groups SET {set_clause} WHERE slug = ?",  # noqa: S608
        values,
    )
    await db.commit()
    return await get_publishing_group(db, slug)


async def delete_publishing_group(db: aiosqlite.Connection, slug: str) -> bool:
    """Delete a non-default publishing group. Returns True if deleted."""
    async with db.execute(
        "SELECT is_default FROM publishing_groups WHERE slug = ?", (slug,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False
    if row[0]:
        raise ValueError(f"Cannot delete default group '{slug}'")

    await db.execute("DELETE FROM publishing_groups WHERE slug = ?", (slug,))
    await db.commit()
    return True


async def get_group_members(db: aiosqlite.Connection, slug: str) -> list[str]:
    """Return entity slugs belonging to a group."""
    members: list[str] = []
    async with db.execute(
        "SELECT entity_slug FROM publishing_group_members "
        "WHERE group_slug = ? ORDER BY entity_slug",
        (slug,),
    ) as cursor:
        async for row in cursor:
            members.append(row[0])
    return members


async def set_group_members(
    db: aiosqlite.Connection, slug: str, entity_slugs: list[str]
) -> list[str]:
    """Replace all members of a group. Returns the new member list."""
    await db.execute(
        "DELETE FROM publishing_group_members WHERE group_slug = ?", (slug,)
    )
    if entity_slugs:
        await db.executemany(
            "INSERT INTO publishing_group_members (group_slug, entity_slug) "
            "VALUES (?, ?)",
            [(slug, s) for s in entity_slugs],
        )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()
    return await get_group_members(db, slug)


async def add_group_member(
    db: aiosqlite.Connection, slug: str, entity_slug: str
) -> None:
    """Add a single entity to a group (idempotent)."""
    await db.execute(
        "INSERT OR IGNORE INTO publishing_group_members (group_slug, entity_slug) "
        "VALUES (?, ?)",
        (slug, entity_slug),
    )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()


async def remove_group_member(
    db: aiosqlite.Connection, slug: str, entity_slug: str
) -> None:
    """Remove a single entity from a group."""
    await db.execute(
        "DELETE FROM publishing_group_members "
        "WHERE group_slug = ? AND entity_slug = ?",
        (slug, entity_slug),
    )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()


async def get_disabled_entity_slugs(db: aiosqlite.Connection) -> set[str]:
    """Return the set of entity slugs that belong to at least one disabled group
    and do NOT belong to any enabled group.

    An entity is considered disabled if every group it belongs to is disabled.
    """
    slugs: set[str] = set()
    async with db.execute(
        "SELECT m.entity_slug "
        "FROM publishing_group_members m "
        "JOIN publishing_groups g ON g.slug = m.group_slug "
        "GROUP BY m.entity_slug "
        "HAVING SUM(g.enabled) = 0"
    ) as cursor:
        async for row in cursor:
            slugs.add(row[0])
    return slugs
