"""SQLite config store with WAL mode and forward-only migrations."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 35

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
    9: """
    -- Multi-gateway: extend gateways table with device info + config
    ALTER TABLE gateways ADD COLUMN description TEXT NOT NULL DEFAULT '';
    ALTER TABLE gateways ADD COLUMN poll_interval INTEGER NOT NULL DEFAULT 10;
    ALTER TABLE gateways ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE gateways ADD COLUMN serial TEXT;
    ALTER TABLE gateways ADD COLUMN model TEXT;
    ALTER TABLE gateways ADD COLUMN firmware TEXT;
    ALTER TABLE gateways ADD COLUMN ac_type INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE gateways ADD COLUMN last_connected_at REAL;

    -- Add gateway_id to control tables
    ALTER TABLE control_log ADD COLUMN gateway_id TEXT NOT NULL DEFAULT 'default';
    ALTER TABLE control_state ADD COLUMN gateway_id TEXT NOT NULL DEFAULT 'default';

    -- Add gateway_id to metrics tables
    ALTER TABLE metrics ADD COLUMN gateway_id TEXT NOT NULL DEFAULT 'default';
    ALTER TABLE metrics_archive ADD COLUMN gateway_id TEXT NOT NULL DEFAULT 'default';

    -- Per-gateway operational stats
    ALTER TABLE operational_stats ADD COLUMN gateway_id TEXT NOT NULL DEFAULT 'default';

    -- Site-level config
    CREATE TABLE IF NOT EXISTS site_config (
        id                INTEGER PRIMARY KEY CHECK (id = 1),
        name              TEXT NOT NULL DEFAULT 'My Site',
        description       TEXT NOT NULL DEFAULT '',
        meter_number      TEXT NOT NULL DEFAULT '',
        account_number    TEXT NOT NULL DEFAULT '',
        ac_service_type   INTEGER NOT NULL DEFAULT 1,
        aggregate_entities INTEGER NOT NULL DEFAULT 1,
        updated_at        REAL NOT NULL DEFAULT 0
    );
    INSERT OR IGNORE INTO site_config (id, updated_at) VALUES (1, 0);
    """,
    10: """
    -- Mock gateways: synthetic data source, no Modbus connection
    ALTER TABLE gateways ADD COLUMN mock INTEGER NOT NULL DEFAULT 0;
    """,
    11: """
    -- Publishing groups: support promoted catalog points as members.
    -- entity_slug doubles as the ref: an entity slug, or "model.point".
    ALTER TABLE publishing_group_members ADD COLUMN member_type TEXT NOT NULL DEFAULT 'entity';
    ALTER TABLE publishing_group_members ADD COLUMN disp_name TEXT;
    ALTER TABLE publishing_group_members ADD COLUMN disp_unit TEXT;
    """,
    12: """
    -- Persist user "Stop" as a transient pause, distinct from admin disable.
    -- autostart=0 means: don't auto-start on app boot (but enabled stays 1,
    -- so it's still a valid, configured gateway the user can start again).
    -- Without this, a stopped gateway (esp. a mock) self-restarts on reboot
    -- because start_all() restarted every enabled row.
    ALTER TABLE gateways ADD COLUMN autostart INTEGER NOT NULL DEFAULT 1;
    """,
    13: """
    -- Electricity Utility Services (Layer 1, customer-declared). A site can
    -- have multiple services / meters, each with its own declared AC type and
    -- rated amperage. Replaces the single site_config.ac_service_type.
    CREATE TABLE IF NOT EXISTS services (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL DEFAULT 'Service 1',
        meter_number  TEXT NOT NULL DEFAULT '',
        account       TEXT NOT NULL DEFAULT '',
        ac_service    INTEGER NOT NULL DEFAULT 1,   -- 1 single / 2 split / 3 three
        rated_amps    INTEGER NOT NULL DEFAULT 0,
        display_order INTEGER NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL DEFAULT 0
    );
    -- Seed one service from the existing single site_config so nothing is lost.
    INSERT OR IGNORE INTO services
        (id, name, meter_number, account, ac_service, rated_amps, display_order, created_at)
    SELECT 'service1', 'Service 1', meter_number, account_number, ac_service_type, 0, 0, 0
    FROM site_config WHERE id = 1;
    """,
    14: """
    -- Gateway → service / phase linkage (Layer 2). 'all' = today's behaviour
    -- (no specific phase). A specific value is 'L1'|'L2'|'L3' (or a combo like
    -- 'L1+L2'), constrained by the gateway's detected ACType.
    ALTER TABLE gateways ADD COLUMN service_id TEXT;
    ALTER TABLE gateways ADD COLUMN phase TEXT NOT NULL DEFAULT 'all';
    """,
    15: """
    -- Per-gateway phase-view preference for a multi-phase aGate (Topology B).
    -- 'both' (default) = today's behaviour: publish aggregate + per-leg entities.
    -- 'aggregate' = suppress the per-leg L1/L2/L3 entities (only on split/three-
    -- phase units; single-phase keeps its L1 set). 'per_phase' = per-leg shown,
    -- aggregate de-emphasised on the dashboard.
    ALTER TABLE gateways ADD COLUMN phase_view TEXT NOT NULL DEFAULT 'both';
    """,
    17: """
    -- Add temperature columns to metrics tables for chart overlays.
    ALTER TABLE metrics ADD COLUMN ambient_temp_c REAL;
    ALTER TABLE metrics ADD COLUMN cabinet_temp_c REAL;
    ALTER TABLE metrics_archive ADD COLUMN ambient_temp_c REAL;
    ALTER TABLE metrics_archive ADD COLUMN cabinet_temp_c REAL;
    """,
    18: """
    -- Add operating mode to metrics for per-segment chart shading and tooltip.
    ALTER TABLE metrics ADD COLUMN mode_name TEXT;
    ALTER TABLE metrics_archive ADD COLUMN mode_name TEXT;
    """,
    19: """
    -- Add reserve percentage columns for historical charting and export.
    ALTER TABLE metrics ADD COLUMN self_reserve_pct INTEGER;
    ALTER TABLE metrics ADD COLUMN tou_reserve_pct INTEGER;
    ALTER TABLE metrics_archive ADD COLUMN self_reserve_pct INTEGER;
    ALTER TABLE metrics_archive ADD COLUMN tou_reserve_pct INTEGER;
    """,
    20: """
    -- gateway_id was already added to metrics_archive in migration 9 (NOT NULL DEFAULT 'default').
    -- This migration is intentionally a no-op; version bump ensures archive INSERT uses it correctly.
    SELECT 1;
    """,
    21: """
    -- Add grid_mode (701.ConnSt decoded: Grid Following/Grid Forming/PV Clipped) for
    -- event-marker rendering on the power history chart.
    ALTER TABLE metrics ADD COLUMN grid_mode TEXT;
    ALTER TABLE metrics_archive ADD COLUMN grid_mode TEXT;
    """,
    22: """
    -- Alarm events: sparse records written only when alarm state changes.
    -- source: 'M701_Alrm' | 'M714_PrtAlrms' | 'M713_Sta'
    -- alarms_set / alarms_cleared: comma-separated human-readable bit names.
    -- severity: 'info' | 'warning' | 'fault'
    CREATE TABLE IF NOT EXISTS alarm_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        gateway_id  TEXT NOT NULL DEFAULT 'default',
        source      TEXT NOT NULL,
        value_raw   INTEGER NOT NULL,
        alarms_set  TEXT NOT NULL DEFAULT '',
        alarms_cleared TEXT NOT NULL DEFAULT '',
        severity    TEXT NOT NULL DEFAULT 'info'
    );
    CREATE INDEX IF NOT EXISTS idx_alarm_events_ts ON alarm_events(ts);
    CREATE INDEX IF NOT EXISTS idx_alarm_events_gw ON alarm_events(gateway_id, ts);
    """,
    23: """
    -- System topology flags for site setup wizard.
    ALTER TABLE site_config ADD COLUMN full_backup       INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE site_config ADD COLUMN grid_forming      INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE site_config ADD COLUMN generator_input   INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE site_config ADD COLUMN solar_type        TEXT    NOT NULL DEFAULT 'none';
    ALTER TABLE site_config ADD COLUMN solar_kwp         REAL    NOT NULL DEFAULT 0;
    ALTER TABLE site_config ADD COLUMN load_shedding     INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE site_config ADD COLUMN nonbackup_loads   INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE site_config ADD COLUMN battery_label     TEXT    NOT NULL DEFAULT '';
    """,
    24: """
    -- Per-gateway Modbus connection timeout (seconds), user-configurable
    -- (default matches the franklinwh-modbus library's own prior default,
    -- so existing gateways see no behaviour change until adjusted).
    ALTER TABLE gateways ADD COLUMN timeout REAL NOT NULL DEFAULT 10;
    """,
    16: """
    -- Scheduler (SCH1): declarative time -> command-handler action entries.
    -- when_spec/params are JSON; action draws from the command vocabulary.
    -- release governs window-exit behaviour (§4a): 'release' hands back to
    -- native TOU/mode, 'hold' keeps a 0W VPP standby to suppress native.
    -- conflict governs what happens when the target is already under control
    -- (§4b): 'defer' (skip), 'override' (preempt), 'wait' (retry in-window).
    CREATE TABLE IF NOT EXISTS schedules (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL DEFAULT 'Schedule',
        enabled     INTEGER NOT NULL DEFAULT 1,
        when_spec   TEXT NOT NULL DEFAULT '{}',   -- {days:[0..6], windows:[{start,end}]}
        action      TEXT NOT NULL DEFAULT 'force_standby',
        params      TEXT NOT NULL DEFAULT '{}',   -- {power_w, power_pct, pct, mode, ...}
        target_type TEXT NOT NULL DEFAULT 'gateway',  -- gateway | service | site
        target_id   TEXT,                          -- nullable for site
        release     TEXT NOT NULL DEFAULT 'release',  -- release | hold
        conflict    TEXT NOT NULL DEFAULT 'defer',    -- defer | override | wait
        priority    INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL DEFAULT 0,
        updated_at  REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS schedule_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        schedule_id TEXT,
        action      TEXT,
        target      TEXT,
        result      TEXT,
        detail      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_schedule_log_ts ON schedule_log(ts);
    """,
    25: """
    -- Scheduler v2 (FWHAI-parity): extend `schedules` with FWHAI-style trigger
    -- kinds, a shared ALL/ANY condition tree used for BOTH entry gates and exit
    -- criteria, a bounded duration, and richer release/missed policies. All
    -- columns are additive with back-compatible defaults so existing SCH1 rows
    -- (which are when_spec-driven) keep firing untouched: a NULL `trigger_kind`
    -- means "evaluate the legacy when_spec", exactly the pre-v25 behaviour.
    --
    -- NOTE: the legacy `release` column (release|hold, window-exit hand-back)
    -- co-exists with the new `release_policy` (release|restore_prior_mode|
    -- set_operating_mode:<v>) rather than being repurposed, to avoid a
    -- destructive rewrite. Reconciling the two is a follow-up, not this
    -- migration.
    ALTER TABLE schedules ADD COLUMN trigger_kind TEXT;
    ALTER TABLE schedules ADD COLUMN trigger_spec TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE schedules ADD COLUMN entry_conditions TEXT;   -- JSON ConditionTree | NULL (always allow)
    ALTER TABLE schedules ADD COLUMN exit_conditions TEXT;    -- JSON ConditionTree | NULL (disabled)
    ALTER TABLE schedules ADD COLUMN duration_s INTEGER;      -- NULL = open-ended
    ALTER TABLE schedules ADD COLUMN release_policy TEXT NOT NULL DEFAULT 'restore_prior_mode';
    ALTER TABLE schedules ADD COLUMN missed_policy TEXT NOT NULL DEFAULT 'late_fire_remaining';
    """,
    26: """
    -- Scheduler v2 Phase 2: first-class connectivity outages. One row per
    -- per-gateway outage (a stretch with no successful poll beyond the
    -- threshold). end_ts/duration_s are filled on recovery; missed_job_ids /
    -- catchup_run_ids link an outage to the schedule fires it caused to be
    -- missed and the catch-up runs that resolved them (populated by catchup).
    CREATE TABLE IF NOT EXISTS outages (
        id              TEXT PRIMARY KEY,
        gateway_id      TEXT NOT NULL,
        start_ts        REAL NOT NULL,
        end_ts          REAL,
        duration_s      REAL,
        reason          TEXT NOT NULL DEFAULT 'stale_reads',
        missed_job_ids  TEXT NOT NULL DEFAULT '[]',
        catchup_run_ids TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS idx_outages_gw_start ON outages(gateway_id, start_ts);
    """,
    27: """
    -- Multiple Home Assistant instances → one Bridge. Each row is an HA
    -- instance the Bridge reads entity states from (inbound), exposed as
    -- `ha:<id>:<entity_id>` automation condition sensors. token = long-lived
    -- access token (NULL → use the Supervisor token for the co-hosted addon).
    CREATE TABLE IF NOT EXISTS ha_instances (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        base_url   TEXT NOT NULL,
        token      TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        enabled    INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    );
    """,
    28: """
    -- Allowlist of HA entities exposed as `ha:<inst>:<entity>` condition
    -- sensors. An HA can have thousands of entities; only rows here reach the
    -- Automation condition namespace. Empty for an instance → nothing exposed.
    CREATE TABLE IF NOT EXISTS ha_exposed_entities (
        instance_id TEXT NOT NULL,
        entity_id   TEXT NOT NULL,
        added_at    REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (instance_id, entity_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ha_exposed_instance
        ON ha_exposed_entities (instance_id);
    """,
    29: """
    -- Condition dwell: entry conditions must hold continuously for this many
    -- seconds before the entry fires (0 = fire immediately). Like HA's `for:`.
    ALTER TABLE schedules ADD COLUMN entry_hold_s INTEGER NOT NULL DEFAULT 0;
    """,
    30: """
    -- One-shot HA-entity actions run when the entry fires (after the battery
    -- action): JSON list of {instance_id, entity_id, service, data}. NULL/[] =
    -- none. Edge-triggered; no revert (see docs/automations-ha-actions...).
    ALTER TABLE schedules ADD COLUMN ha_actions TEXT;
    """,
    31: """
    -- Persisted application logs (INFO+), so the Logs tab survives restarts and
    -- can be filtered by time span. The in-memory ring buffer stays the fast
    -- "recent" view; this table is the searchable history (with retention).
    CREATE TABLE IF NOT EXISTS logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         REAL NOT NULL,
        level      TEXT NOT NULL,
        name       TEXT,
        message    TEXT,
        gateway_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (ts);
    CREATE INDEX IF NOT EXISTS idx_logs_level ON logs (level);
    """,
    32: """
    -- User accounts + roles (multi-user Phase 1). Sessions ride signed cookies
    -- (no table). password_hash = argon2id. An admin is seeded on first startup.
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'viewer'
                        CHECK (role IN ('admin','user','viewer')),
        enabled       INTEGER NOT NULL DEFAULT 1,
        created_at    REAL NOT NULL DEFAULT 0,
        updated_at    REAL NOT NULL DEFAULT 0,
        last_login_at REAL
    );
    """,
    33: """
    -- Utility-service billing/tariff parameters. Flags gate whether each applies;
    -- demand_window/bonus_window are JSON {months:[1..12], days:[0..6 Mon=0],
    -- start:"HH:MM", end:"HH:MM"}; pricing is a free-form JSON of rate params.
    ALTER TABLE services ADD COLUMN has_tou INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE services ADD COLUMN has_peak_demand INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE services ADD COLUMN has_export_bonus INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE services ADD COLUMN min_monthly_bill REAL NOT NULL DEFAULT 0;
    ALTER TABLE services ADD COLUMN pricing_api TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE services ADD COLUMN demand_window TEXT;
    ALTER TABLE services ADD COLUMN bonus_window TEXT;
    ALTER TABLE services ADD COLUMN pricing TEXT;
    """,
    34: """
    -- Billing-period history (tariff reporting, Phase C). One row per closed
    -- billing period, snapshotted by the DemandTracker at each cycle rollover.
    -- period_start/end are unix ts; UNIQUE(gateway_id, period_start) makes the
    -- snapshot idempotent across restarts / re-fires.
    CREATE TABLE IF NOT EXISTS billing_periods (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        gateway_id     TEXT NOT NULL DEFAULT 'default',
        period_start   REAL NOT NULL,
        period_end     REAL NOT NULL,
        demand_peak_kw REAL NOT NULL DEFAULT 0,
        demand_charge  REAL NOT NULL DEFAULT 0,
        reward_kwh     REAL NOT NULL DEFAULT 0,
        reward_credit  REAL NOT NULL DEFAULT 0,
        charge_kwh     REAL NOT NULL DEFAULT 0,
        charge_net_kwh REAL NOT NULL DEFAULT 0,
        charge_cost    REAL NOT NULL DEFAULT 0,
        net_total      REAL NOT NULL DEFAULT 0,
        created_at     REAL NOT NULL DEFAULT 0,
        UNIQUE(gateway_id, period_start)
    );
    """,
    35: """
    -- Fixed / standing charges accrued over the period (daily supply, metering,
    -- membership, …). Folded into net_total from here on; rows written before
    -- this migration keep 0, so their net_total is usage-driven cost only.
    ALTER TABLE billing_periods ADD COLUMN fixed_charges REAL NOT NULL DEFAULT 0;
    """,
}


async def get_schema_version(db: aiosqlite.Connection) -> int:
    """Get the current schema version, or 0 if no schema exists."""
    try:
        async with db.execute("SELECT MAX(version) FROM schema_version") as cursor:
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
    # Set row_factory globally so all cursors return sqlite3.Row objects.
    # sqlite3.Row supports both integer index (row[0]) and string key (row["col"])
    # access, so all existing code works unchanged.  Setting it once here avoids
    # a race condition: the per-function set/finally-reset pattern was racy
    # because another coroutine could reset db.row_factory to None between
    # execute() and fetchone(), causing fetchone() to return a plain tuple.
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await run_migrations(db)

    # Safety: ensure metrics_archive exists even if v7 was applied before
    # the table was added to that migration's DDL.
    await db.executescript("""
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
    """)

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
    gateway_id: str = "default",
) -> None:
    import json

    hw_json = json.dumps(hw_state) if hw_state else None
    await db.execute(
        "INSERT INTO control_log (ts, event, action, power_w, detail, hw_state_json, gateway_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), event, action, power_w, detail, hw_json, gateway_id),
    )
    await db.commit()


async def save_control_state(
    db: aiosqlite.Connection,
    active: bool,
    action: str = "",
    power_w: int = 0,
    started_at: float = 0,
    watchdog_s: int = 3600,
    gateway_id: str = "default",
) -> None:
    # Upsert: update existing row for this gateway, or insert if missing
    row_id = 1 if gateway_id == "default" else abs(hash(gateway_id)) % 2**31
    await db.execute(
        "INSERT INTO control_state "
        "(id, active, action, power_w, started_at, watchdog_s, "
        " updated_at, gateway_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "active=excluded.active, action=excluded.action, "
        "power_w=excluded.power_w, started_at=excluded.started_at, "
        "watchdog_s=excluded.watchdog_s, updated_at=excluded.updated_at, "
        "gateway_id=excluded.gateway_id",
        (row_id, int(active), action, power_w, started_at, watchdog_s, time.time(), gateway_id),
    )
    await db.commit()


async def load_control_state(
    db: aiosqlite.Connection,
    gateway_id: str = "default",
) -> dict:
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT * FROM control_state WHERE gateway_id=?", (gateway_id,)
        ) as cur:
            row = await cur.fetchone()
    finally:
        db.row_factory = aiosqlite.Row
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


# ── Gateway CRUD ──────────────────────────────────────────────


async def get_gateways(db: aiosqlite.Connection) -> list[dict]:
    """List all gateways."""
    db.row_factory = aiosqlite.Row
    try:
        rows = []
        async with db.execute("SELECT * FROM gateways ORDER BY display_order, id") as cursor:
            async for row in cursor:
                rows.append(dict(row))
        return rows
    finally:
        db.row_factory = aiosqlite.Row


async def get_gateway(db: aiosqlite.Connection, gateway_id: str) -> dict | None:
    """Get a single gateway by ID."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM gateways WHERE id = ?", (gateway_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
    finally:
        db.row_factory = aiosqlite.Row


async def create_gateway(
    db: aiosqlite.Connection,
    gateway_id: str,
    name: str,
    host: str,
    port: int = 502,
    unit_id: int = 1,
    description: str = "",
    poll_interval: int = 10,
    timeout: float = 10.0,
    mock: bool = False,
) -> dict:
    """Create a new gateway."""
    now = time.time()
    # Find next display_order
    async with db.execute("SELECT COALESCE(MAX(display_order), -1) + 1 FROM gateways") as cur:
        order = (await cur.fetchone())[0]

    await db.execute(
        "INSERT INTO gateways (id, name, host, port, unit_id, enabled, created_at, "
        "description, poll_interval, timeout, display_order, mock) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            gateway_id,
            name,
            host,
            port,
            unit_id,
            now,
            description,
            poll_interval,
            timeout,
            order,
            int(mock),
        ),
    )
    await db.commit()
    return await get_gateway(db, gateway_id)  # type: ignore[return-value]


async def update_gateway(
    db: aiosqlite.Connection,
    gateway_id: str,
    **kwargs: object,
) -> dict | None:
    """Update gateway fields. Accepts any column name as keyword arg."""
    existing = await get_gateway(db, gateway_id)
    if existing is None:
        return None
    if not kwargs:
        return existing

    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values())
    values.append(gateway_id)
    await db.execute(
        f"UPDATE gateways SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )
    await db.commit()
    return await get_gateway(db, gateway_id)


async def delete_gateway(db: aiosqlite.Connection, gateway_id: str) -> bool:
    """Delete a gateway, its catalog, and its metrics. Returns True if deleted.

    The FK children (``device_points`` → ``device_models`` → ``gateways``, and
    ``gateway_state``) have no ON DELETE CASCADE, so a gateway that captured a
    SunSpec catalog would otherwise fail with a FOREIGN KEY constraint error.
    Delete them in dependency order first, then purge metrics, then the row.
    """
    if gateway_id == "default":
        raise ValueError("Cannot delete the default gateway")

    # device_points references device_models — delete points before models.
    with contextlib.suppress(aiosqlite.OperationalError):
        await db.execute(
            "DELETE FROM device_points WHERE model_db_id IN "
            "(SELECT id FROM device_models WHERE gateway_id = ?)",
            (gateway_id,),
        )
    # Remaining FK children + metrics (no FK, but clean them up too).
    for table in ("device_models", "gateway_state", "metrics", "metrics_archive"):
        with contextlib.suppress(aiosqlite.OperationalError):
            await db.execute(
                f"DELETE FROM {table} WHERE gateway_id = ?",  # noqa: S608
                (gateway_id,),
            )

    cur = await db.execute("DELETE FROM gateways WHERE id = ?", (gateway_id,))
    await db.commit()
    return cur.rowcount > 0


# ── Site config ───────────────────────────────────────────────


async def get_site_config(db: aiosqlite.Connection) -> dict:
    """Get site configuration."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM site_config WHERE id = 1") as cur:
            row = await cur.fetchone()
            if row is None:
                return {
                    "name": "My Site",
                    "description": "",
                    "meter_number": "",
                    "account_number": "",
                    "ac_service_type": 1,
                    "aggregate_entities": True,
                }
            return {
                "name": row["name"],
                "description": row["description"],
                "meter_number": row["meter_number"],
                "account_number": row["account_number"],
                "ac_service_type": row["ac_service_type"],
                "aggregate_entities": bool(row["aggregate_entities"]),
                "updated_at": row["updated_at"],
                "full_backup": bool(row["full_backup"]) if "full_backup" in row.keys() else False,
                "grid_forming": bool(row["grid_forming"])
                if "grid_forming" in row.keys()
                else False,
                "generator_input": bool(row["generator_input"])
                if "generator_input" in row.keys()
                else False,
                "solar_type": row["solar_type"] if "solar_type" in row.keys() else "none",
                "solar_kwp": float(row["solar_kwp"]) if "solar_kwp" in row.keys() else 0.0,
                "load_shedding": bool(row["load_shedding"])
                if "load_shedding" in row.keys()
                else False,
                "nonbackup_loads": bool(row["nonbackup_loads"])
                if "nonbackup_loads" in row.keys()
                else False,
                "battery_label": row["battery_label"] if "battery_label" in row.keys() else "",
            }
    finally:
        db.row_factory = aiosqlite.Row


async def update_site_config(db: aiosqlite.Connection, **kwargs: object) -> dict:
    """Update site configuration fields."""
    if not kwargs:
        return await get_site_config(db)
    kwargs["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values())
    await db.execute(
        f"UPDATE site_config SET {set_clause} WHERE id = 1",  # noqa: S608
        values,
    )
    await db.commit()
    return await get_site_config(db)


# ── Electricity Utility Services (Layer 1) ────────────────────────

# Mutable service columns. Billing/tariff params added in migration 33.
_SERVICE_FIELDS = (
    "name", "meter_number", "account", "ac_service", "rated_amps",
    "has_tou", "has_peak_demand", "has_export_bonus", "min_monthly_bill", "pricing_api",
    "demand_window", "bonus_window", "pricing",
)
_SERVICE_JSON_FIELDS = ("demand_window", "bonus_window", "pricing")


def _decode_service(row: dict) -> dict:
    """Decode the JSON billing columns to objects (None stays None)."""
    for f in _SERVICE_JSON_FIELDS:
        raw = row.get(f)
        if isinstance(raw, str) and raw:
            try:
                row[f] = json.loads(raw)
            except (ValueError, TypeError):
                row[f] = None
    return row


async def get_services(db: aiosqlite.Connection) -> list[dict]:
    """List all electricity utility services, in display order."""
    db.row_factory = aiosqlite.Row
    try:
        rows = []
        async with db.execute(
            "SELECT * FROM services ORDER BY display_order, created_at, id"
        ) as cur:
            async for row in cur:
                rows.append(_decode_service(dict(row)))
        return rows
    finally:
        db.row_factory = aiosqlite.Row


async def get_service(db: aiosqlite.Connection, service_id: str) -> dict | None:
    """Get a single service by ID."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM services WHERE id = ?", (service_id,)) as cur:
            row = await cur.fetchone()
            return _decode_service(dict(row)) if row else None
    finally:
        db.row_factory = aiosqlite.Row


async def create_service(
    db: aiosqlite.Connection,
    name: str,
    meter_number: str = "",
    account: str = "",
    ac_service: int = 1,
    rated_amps: int = 0,
) -> dict:
    """Create a new utility service (core fields; billing set via update)."""
    import uuid

    service_id = f"svc_{uuid.uuid4().hex[:8]}"
    now = time.time()
    async with db.execute("SELECT COALESCE(MAX(display_order), -1) + 1 FROM services") as cur:
        order = (await cur.fetchone())[0]
    await db.execute(
        "INSERT INTO services "
        "(id, name, meter_number, account, ac_service, rated_amps, display_order, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (service_id, name, meter_number, account, int(ac_service), int(rated_amps), order, now),
    )
    await db.commit()
    return await get_service(db, service_id)  # type: ignore[return-value]


async def update_service(
    db: aiosqlite.Connection, service_id: str, **kwargs: object
) -> dict | None:
    """Update a service. Only known columns are applied; JSON billing fields are
    encoded (None → SQL NULL)."""
    existing = await get_service(db, service_id)
    if existing is None:
        return None
    updates = {k: v for k, v in kwargs.items() if k in _SERVICE_FIELDS}
    if not updates:
        return existing
    for f in _SERVICE_JSON_FIELDS:
        if f in updates and updates[f] is not None and not isinstance(updates[f], str):
            updates[f] = json.dumps(updates[f])
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())
    values.append(service_id)
    await db.execute(
        f"UPDATE services SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )
    await db.commit()
    return await get_service(db, service_id)


async def delete_service(db: aiosqlite.Connection, service_id: str) -> bool:
    """Delete a service. Returns True if a row was removed."""
    cur = await db.execute("DELETE FROM services WHERE id = ?", (service_id,))
    await db.commit()
    return cur.rowcount > 0


# ── Scheduler (SCH1) ──────────────────────────────────────────

# Mutable columns a PATCH may touch. id/created_at are immutable.
_SCHEDULE_FIELDS = (
    "name",
    "enabled",
    "when_spec",
    "action",
    "params",
    "target_type",
    "target_id",
    "release",
    "conflict",
    "priority",
    # v2 fields
    "trigger_kind",
    "trigger_spec",
    "entry_conditions",
    "exit_conditions",
    "duration_s",
    "release_policy",
    "missed_policy",
    "entry_hold_s",
    "ha_actions",
)
# Columns stored as JSON text but always surfaced as dicts (default {} when absent).
_SCHEDULE_JSON_FIELDS = ("when_spec", "params", "trigger_spec")
# JSON columns surfaced as lists (default [] when absent/NULL).
_SCHEDULE_LIST_JSON_FIELDS = ("ha_actions",)
# JSON columns that are meaningfully nullable: NULL decodes to None (not {}),
# because for a condition tree "no gate / disabled" differs from "empty tree".
_SCHEDULE_NULLABLE_JSON_FIELDS = ("entry_conditions", "exit_conditions")


def _decode_schedule(row: dict) -> dict:
    """Turn a raw DB row into an API-shaped dict (JSON fields → objects)."""
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    for f in _SCHEDULE_JSON_FIELDS:
        raw = d.get(f) or "{}"
        try:
            d[f] = json.loads(raw)
        except (ValueError, TypeError):
            d[f] = {}
    for f in _SCHEDULE_NULLABLE_JSON_FIELDS:
        raw = d.get(f)
        if raw in (None, ""):
            d[f] = None
        else:
            try:
                d[f] = json.loads(raw)
            except (ValueError, TypeError):
                d[f] = None
    for f in _SCHEDULE_LIST_JSON_FIELDS:
        raw = d.get(f)
        if raw in (None, ""):
            d[f] = []
        else:
            try:
                d[f] = json.loads(raw)
            except (ValueError, TypeError):
                d[f] = []
    return d


async def get_schedules(db: aiosqlite.Connection) -> list[dict]:
    """List all schedule entries, highest priority first then newest."""
    db.row_factory = aiosqlite.Row
    try:
        rows = []
        async with db.execute(
            # priority first, then OLDEST first: at equal priority the
            # longest-standing entry outranks one added later, so adding a rule
            # never silently displaces an established one. Reordering is done by
            # setting priority explicitly.
            "SELECT * FROM schedules ORDER BY priority DESC, created_at ASC, id"
        ) as cur:
            async for row in cur:
                rows.append(_decode_schedule(dict(row)))
        return rows
    finally:
        db.row_factory = aiosqlite.Row


async def get_schedule(db: aiosqlite.Connection, schedule_id: str) -> dict | None:
    """Get a single schedule by ID."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)) as cur:
            row = await cur.fetchone()
            return _decode_schedule(dict(row)) if row else None
    finally:
        db.row_factory = aiosqlite.Row


async def create_schedule(
    db: aiosqlite.Connection,
    name: str,
    when_spec: dict,
    action: str,
    params: dict | None = None,
    target_type: str = "gateway",
    target_id: str | None = None,
    enabled: bool = True,
    release: str = "release",
    conflict: str = "defer",
    priority: int = 0,
    *,
    trigger_kind: str | None = None,
    trigger_spec: dict | None = None,
    entry_conditions: dict | None = None,
    exit_conditions: dict | None = None,
    duration_s: int | None = None,
    release_policy: str = "restore_prior_mode",
    missed_policy: str = "late_fire_remaining",
    entry_hold_s: int = 0,
    ha_actions: list | None = None,
) -> dict:
    """Create a schedule entry. Returns the created (decoded) row."""
    import uuid

    schedule_id = f"sch_{uuid.uuid4().hex[:8]}"
    now = time.time()
    await db.execute(
        "INSERT INTO schedules "
        "(id, name, enabled, when_spec, action, params, target_type, target_id, "
        " release, conflict, priority, created_at, updated_at, "
        " trigger_kind, trigger_spec, entry_conditions, exit_conditions, "
        " duration_s, release_policy, missed_policy, entry_hold_s, ha_actions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schedule_id,
            name,
            int(enabled),
            json.dumps(when_spec),
            action,
            json.dumps(params or {}),
            target_type,
            target_id,
            release,
            conflict,
            int(priority),
            now,
            now,
            trigger_kind,
            json.dumps(trigger_spec or {}),
            json.dumps(entry_conditions) if entry_conditions is not None else None,
            json.dumps(exit_conditions) if exit_conditions is not None else None,
            duration_s,
            release_policy,
            missed_policy,
            int(entry_hold_s or 0),
            json.dumps(ha_actions or []),
        ),
    )
    await db.commit()
    return await get_schedule(db, schedule_id)  # type: ignore[return-value]


async def update_schedule(
    db: aiosqlite.Connection, schedule_id: str, **kwargs: object
) -> dict | None:
    """Update a schedule. Only known columns are applied; dict fields JSON-encoded."""
    existing = await get_schedule(db, schedule_id)
    if existing is None:
        return None
    updates = {k: v for k, v in kwargs.items() if k in _SCHEDULE_FIELDS}
    if not updates:
        return existing
    for f in _SCHEDULE_JSON_FIELDS:
        if f in updates and not isinstance(updates[f], str):
            updates[f] = json.dumps(updates[f])
    for f in _SCHEDULE_LIST_JSON_FIELDS:
        if f in updates and not isinstance(updates[f], str):
            updates[f] = json.dumps(updates[f] or [])
    # Nullable JSON (condition trees): encode dicts, but leave None as SQL NULL.
    for f in _SCHEDULE_NULLABLE_JSON_FIELDS:
        if f in updates and updates[f] is not None and not isinstance(updates[f], str):
            updates[f] = json.dumps(updates[f])
    if "enabled" in updates:
        updates["enabled"] = int(bool(updates["enabled"]))
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())
    values.append(schedule_id)
    await db.execute(
        f"UPDATE schedules SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )
    await db.commit()
    return await get_schedule(db, schedule_id)


async def delete_schedule(db: aiosqlite.Connection, schedule_id: str) -> bool:
    """Delete a schedule entry. Returns True if a row was removed."""
    cur = await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    await db.commit()
    return cur.rowcount > 0


async def log_schedule_event(
    db: aiosqlite.Connection,
    schedule_id: str | None,
    action: str,
    target: str,
    result: str,
    detail: str = "",
) -> None:
    """Append an audit row to schedule_log."""
    await db.execute(
        "INSERT INTO schedule_log (ts, schedule_id, action, target, result, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), schedule_id, action, target, result, detail),
    )
    await db.commit()


async def get_schedule_log(
    db: aiosqlite.Connection,
    limit: int = 100,
    schedule_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Return the most recent schedule_log rows, newest first, optionally
    filtered by ``schedule_id`` and/or ``status`` (the audit ``result``)."""
    db.row_factory = aiosqlite.Row
    clauses, args = [], []
    if schedule_id is not None:
        clauses.append("schedule_id = ?")
        args.append(schedule_id)
    if status is not None:
        clauses.append("result = ?")
        args.append(status)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    args.append(limit)
    try:
        rows = []
        async with db.execute(
            f"SELECT * FROM schedule_log {where}ORDER BY ts DESC, id DESC LIMIT ?",  # noqa: S608
            args,
        ) as cur:
            async for row in cur:
                rows.append(dict(row))
        return rows
    finally:
        db.row_factory = aiosqlite.Row


# ── Generic app_config key/value ─────────────────────────────


async def set_app_config(db: aiosqlite.Connection, key: str, value: str) -> None:
    """Upsert a value into the generic app_config key/value store."""
    await db.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def get_app_config(
    db: aiosqlite.Connection, key: str, default: str | None = None
) -> str | None:
    """Read a value from the app_config key/value store, or ``default``."""
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT value FROM app_config WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
        return row["value"] if row else default


# ── Billing-period history (tariff reporting, Phase C) ───────

_BILLING_PERIOD_FIELDS = (
    "gateway_id", "period_start", "period_end", "demand_peak_kw", "demand_charge",
    "reward_kwh", "reward_credit", "charge_kwh", "charge_net_kwh", "charge_cost",
    "fixed_charges", "net_total", "created_at",
)


async def insert_billing_period(db: aiosqlite.Connection, record: dict) -> None:
    """Persist one closed billing period. Idempotent on (gateway_id,
    period_start) — a re-snapshot of the same period is ignored. Fields the
    caller omits fall back to 0 (the columns are NOT NULL)."""
    cols = ", ".join(_BILLING_PERIOD_FIELDS)
    placeholders = ", ".join("?" for _ in _BILLING_PERIOD_FIELDS)
    await db.execute(
        f"INSERT INTO billing_periods ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT(gateway_id, period_start) DO NOTHING",
        tuple(record.get(f, 0) for f in _BILLING_PERIOD_FIELDS),
    )
    await db.commit()


async def get_billing_periods(
    db: aiosqlite.Connection, gateway_id: str = "default", limit: int = 36
) -> list[dict]:
    """Closed billing periods for a gateway, most-recent first."""
    db.row_factory = aiosqlite.Row
    rows = []
    async with db.execute(
        "SELECT * FROM billing_periods WHERE gateway_id = ? "
        "ORDER BY period_start DESC LIMIT ?",
        (gateway_id, limit),
    ) as cur:
        async for row in cur:
            rows.append(dict(row))
    return rows


# ── HA instances (multi-HA entity access) ────────────────────

_HA_FIELDS = ("name", "base_url", "token", "is_default", "enabled")


def _decode_ha(row: dict) -> dict:
    d = dict(row)
    d["is_default"] = bool(d.get("is_default", 0))
    d["enabled"] = bool(d.get("enabled", 1))
    return d


async def get_ha_instances(db: aiosqlite.Connection) -> list[dict]:
    db.row_factory = aiosqlite.Row
    rows = []
    async with db.execute("SELECT * FROM ha_instances ORDER BY is_default DESC, name") as cur:
        async for row in cur:
            rows.append(_decode_ha(dict(row)))
    return rows


async def get_ha_instance(db: aiosqlite.Connection, ha_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM ha_instances WHERE id = ?", (ha_id,)) as cur:
        row = await cur.fetchone()
        return _decode_ha(dict(row)) if row else None


async def create_ha_instance(
    db: aiosqlite.Connection,
    name: str,
    base_url: str,
    token: str | None = None,
    is_default: bool = False,
    enabled: bool = True,
) -> dict:
    import uuid

    ha_id = f"ha_{uuid.uuid4().hex[:8]}"
    now = time.time()
    if is_default:  # only one default
        await db.execute("UPDATE ha_instances SET is_default = 0")
    await db.execute(
        "INSERT INTO ha_instances "
        "(id, name, base_url, token, is_default, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ha_id, name, base_url.rstrip("/"), token, int(is_default), int(enabled), now, now),
    )
    await db.commit()
    return await get_ha_instance(db, ha_id)  # type: ignore[return-value]


async def update_ha_instance(db: aiosqlite.Connection, ha_id: str, **kwargs: object) -> dict | None:
    existing = await get_ha_instance(db, ha_id)
    if existing is None:
        return None
    updates = {k: v for k, v in kwargs.items() if k in _HA_FIELDS}
    if not updates:
        return existing
    if "base_url" in updates and isinstance(updates["base_url"], str):
        updates["base_url"] = updates["base_url"].rstrip("/")
    for f in ("is_default", "enabled"):
        if f in updates:
            updates[f] = int(bool(updates[f]))
    if updates.get("is_default"):
        await db.execute("UPDATE ha_instances SET is_default = 0")
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = [*updates.values(), ha_id]
    await db.execute(f"UPDATE ha_instances SET {set_clause} WHERE id = ?", values)  # noqa: S608
    await db.commit()
    return await get_ha_instance(db, ha_id)


async def delete_ha_instance(db: aiosqlite.Connection, ha_id: str) -> bool:
    cur = await db.execute("DELETE FROM ha_instances WHERE id = ?", (ha_id,))
    # Cascade: drop the instance's exposed-entity allowlist too.
    await db.execute("DELETE FROM ha_exposed_entities WHERE instance_id = ?", (ha_id,))
    await db.commit()
    return cur.rowcount > 0


# ── HA exposed-entity allowlist (which entities become ha:* sensors) ──


async def get_exposed_entities(db: aiosqlite.Connection, instance_id: str) -> set[str]:
    """Entity ids exposed for one instance."""
    db.row_factory = aiosqlite.Row
    out: set[str] = set()
    async with db.execute(
        "SELECT entity_id FROM ha_exposed_entities WHERE instance_id = ?", (instance_id,)
    ) as cur:
        async for row in cur:
            out.add(row["entity_id"])
    return out


async def get_all_exposed_entities(db: aiosqlite.Connection) -> dict[str, set[str]]:
    """All allowlists keyed by instance_id → {entity_id, ...}."""
    db.row_factory = aiosqlite.Row
    out: dict[str, set[str]] = {}
    async with db.execute("SELECT instance_id, entity_id FROM ha_exposed_entities") as cur:
        async for row in cur:
            out.setdefault(row["instance_id"], set()).add(row["entity_id"])
    return out


# ── Persisted application logs ───────────────────────────────


async def insert_logs(db: aiosqlite.Connection, rows: list[dict]) -> int:
    """Batch-insert log rows (dicts: ts, level, name, message, gateway_id)."""
    if not rows:
        return 0
    await db.executemany(
        "INSERT INTO logs (ts, level, name, message, gateway_id) VALUES (?, ?, ?, ?, ?)",
        [
            (r.get("ts"), r.get("level"), r.get("name"), r.get("message"),
             r.get("gateway_id") or "")
            for r in rows
        ],
    )
    await db.commit()
    return len(rows)


async def query_logs(
    db: aiosqlite.Connection,
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    level: str | None = None,
    gateway_id: str | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Filtered log history (newest first) + total match count for pagination."""
    db.row_factory = aiosqlite.Row
    where: list[str] = []
    params: list = []
    if start_ts is not None:
        where.append("ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("ts <= ?")
        params.append(end_ts)
    if level:
        where.append("level = ?")
        params.append(level.upper())
    if gateway_id:
        where.append("gateway_id = ?")
        params.append(gateway_id)
    if source:
        where.append("name = ?")
        params.append(source)
    if search:
        where.append("message LIKE ?")
        params.append(f"%{search}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    async with db.execute(f"SELECT COUNT(*) AS n FROM logs{clause}", params) as cur:  # noqa: S608
        total = (await cur.fetchone())["n"]
    rows = []
    async with db.execute(
        f"SELECT ts, level, name, message, gateway_id FROM logs{clause} "  # noqa: S608
        "ORDER BY ts DESC LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 2000)), max(0, offset)],
    ) as cur:
        async for row in cur:
            rows.append(dict(row))
    return {"logs": rows, "total": total}


async def purge_logs(db: aiosqlite.Connection, older_than_ts: float) -> int:
    """Delete log rows older than a cutoff (retention). Returns rows removed."""
    cur = await db.execute("DELETE FROM logs WHERE ts < ?", (older_than_ts,))
    await db.commit()
    return cur.rowcount


async def log_sources(db: aiosqlite.Connection) -> list[str]:
    """Distinct logger names present (for the Source filter dropdown)."""
    db.row_factory = aiosqlite.Row
    out = []
    sql = "SELECT DISTINCT name FROM logs WHERE name IS NOT NULL ORDER BY name"
    async with db.execute(sql) as cur:
        async for row in cur:
            out.append(row["name"])
    return out


# ── Users (multi-user Phase 1) ───────────────────────────────

_USER_FIELDS = ("username", "password_hash", "role", "enabled")


def _decode_user(row: dict) -> dict:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    return d


async def count_users(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COUNT(*) AS n FROM users") as cur:
        return (await cur.fetchone())[0]


async def get_users(db: aiosqlite.Connection) -> list[dict]:
    db.row_factory = aiosqlite.Row
    rows = []
    async with db.execute("SELECT * FROM users ORDER BY username") as cur:
        async for row in cur:
            rows.append(_decode_user(dict(row)))
    return rows


async def get_user(db: aiosqlite.Connection, user_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
        return _decode_user(dict(row)) if row else None


async def get_user_by_username(db: aiosqlite.Connection, username: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM users WHERE username = ?", (username,)) as cur:
        row = await cur.fetchone()
        return _decode_user(dict(row)) if row else None


async def create_user(
    db: aiosqlite.Connection,
    username: str,
    password_hash: str,
    role: str = "viewer",
    enabled: bool = True,
) -> dict:
    import uuid

    user_id = f"user_{uuid.uuid4().hex[:8]}"
    now = time.time()
    await db.execute(
        "INSERT INTO users (id, username, password_hash, role, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, password_hash, role, int(enabled), now, now),
    )
    await db.commit()
    return await get_user(db, user_id)  # type: ignore[return-value]


async def update_user(db: aiosqlite.Connection, user_id: str, **kwargs: object) -> dict | None:
    existing = await get_user(db, user_id)
    if existing is None:
        return None
    allowed = (*_USER_FIELDS, "last_login_at")
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return existing
    if "enabled" in updates:
        updates["enabled"] = int(bool(updates["enabled"]))
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    await db.execute(
        f"UPDATE users SET {set_clause} WHERE id = ?",  # noqa: S608
        [*updates.values(), user_id],
    )
    await db.commit()
    return await get_user(db, user_id)


async def delete_user(db: aiosqlite.Connection, user_id: str) -> bool:
    cur = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return cur.rowcount > 0


async def set_entity_exposed(
    db: aiosqlite.Connection, instance_id: str, entity_id: str, exposed: bool
) -> bool:
    """Add/remove one entity from an instance's allowlist. Returns the new state."""
    if exposed:
        await db.execute(
            "INSERT OR IGNORE INTO ha_exposed_entities (instance_id, entity_id, added_at) "
            "VALUES (?, ?, ?)",
            (instance_id, entity_id, time.time()),
        )
    else:
        await db.execute(
            "DELETE FROM ha_exposed_entities WHERE instance_id = ? AND entity_id = ?",
            (instance_id, entity_id),
        )
    await db.commit()
    return exposed


# ── Connectivity outages (scheduler v2 Phase 2) ──────────────

_OUTAGE_JSON_FIELDS = ("missed_job_ids", "catchup_run_ids")


def _decode_outage(row: dict) -> dict:
    d = dict(row)
    for f in _OUTAGE_JSON_FIELDS:
        raw = d.get(f) or "[]"
        try:
            d[f] = json.loads(raw)
        except (ValueError, TypeError):
            d[f] = []
    return d


async def create_outage(
    db: aiosqlite.Connection,
    gateway_id: str,
    start_ts: float,
    reason: str = "stale_reads",
) -> str:
    """Open a new outage for a gateway. Returns the new outage id."""
    import uuid

    outage_id = f"out_{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO outages (id, gateway_id, start_ts, reason) VALUES (?, ?, ?, ?)",
        (outage_id, gateway_id, start_ts, reason),
    )
    await db.commit()
    return outage_id


async def close_outage(db: aiosqlite.Connection, outage_id: str, end_ts: float) -> dict | None:
    """Close an outage: set end_ts and duration_s. Returns the decoded row."""
    row = await get_outage(db, outage_id)
    if row is None:
        return None
    duration = max(0.0, end_ts - float(row["start_ts"]))
    await db.execute(
        "UPDATE outages SET end_ts = ?, duration_s = ? WHERE id = ?",
        (end_ts, duration, outage_id),
    )
    await db.commit()
    return await get_outage(db, outage_id)


async def get_outage(db: aiosqlite.Connection, outage_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM outages WHERE id = ?", (outage_id,)) as cur:
        row = await cur.fetchone()
        return _decode_outage(dict(row)) if row else None


async def get_open_outage(db: aiosqlite.Connection, gateway_id: str) -> dict | None:
    """The currently-open (end_ts NULL) outage for a gateway, if any."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM outages WHERE gateway_id = ? AND end_ts IS NULL "
        "ORDER BY start_ts DESC LIMIT 1",
        (gateway_id,),
    ) as cur:
        row = await cur.fetchone()
        return _decode_outage(dict(row)) if row else None


async def get_recent_outages(
    db: aiosqlite.Connection, limit: int = 50, gateway_id: str | None = None
) -> list[dict]:
    """Most recent outages (newest first), optionally filtered by gateway."""
    db.row_factory = aiosqlite.Row
    if gateway_id is not None:
        sql = "SELECT * FROM outages WHERE gateway_id = ? ORDER BY start_ts DESC LIMIT ?"
        args: tuple = (gateway_id, limit)
    else:
        sql = "SELECT * FROM outages ORDER BY start_ts DESC LIMIT ?"
        args = (limit,)
    rows = []
    async with db.execute(sql, args) as cur:
        async for row in cur:
            rows.append(_decode_outage(dict(row)))
    return rows


async def set_outage_catchup(
    db: aiosqlite.Connection,
    outage_id: str,
    missed_job_ids: list[str],
    catchup_run_ids: list[str],
) -> None:
    """Record which jobs an outage caused to be missed and their catch-up runs."""
    await db.execute(
        "UPDATE outages SET missed_job_ids = ?, catchup_run_ids = ? WHERE id = ?",
        (json.dumps(missed_job_ids), json.dumps(catchup_run_ids), outage_id),
    )
    await db.commit()


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
                rows.append(
                    {
                        "model_id": row["model_id"],
                        "point_name": row["point_name"],
                        "status": row["status"],
                        "notes": row["notes"],
                        "updated_at": row["updated_at"],
                    }
                )
        return rows
    finally:
        db.row_factory = aiosqlite.Row


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
    "host",
    "port",
    "username",
    "password",
    "tls_mode",
    "enabled",
    "client_id",
    "qos",
    "retain_discovery",
    "topic_prefix",
    "discovery_prefix",
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
        db.row_factory = aiosqlite.Row

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
                rows.append(
                    {
                        "slug": row["slug"],
                        "name": row["name"],
                        "description": row["description"],
                        "enabled": bool(row["enabled"]),
                        "is_default": bool(row["is_default"]),
                        "sort_order": row["sort_order"],
                        "member_count": row["member_count"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )
        return rows
    finally:
        db.row_factory = aiosqlite.Row


async def get_publishing_group(db: aiosqlite.Connection, slug: str) -> dict | None:
    """Get a single publishing group with its member slugs."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM publishing_groups WHERE slug = ?", (slug,)) as cursor:
            row = await cursor.fetchone()
    finally:
        db.row_factory = aiosqlite.Row

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
    """Return curated-entity slugs belonging to a group (excludes promoted points)."""
    members: list[str] = []
    async with db.execute(
        "SELECT entity_slug FROM publishing_group_members "
        "WHERE group_slug = ? AND member_type = 'entity' ORDER BY entity_slug",
        (slug,),
    ) as cursor:
        async for row in cursor:
            members.append(row[0])
    return members


async def set_group_members(
    db: aiosqlite.Connection, slug: str, entity_slugs: list[str]
) -> list[str]:
    """Replace a group's curated-entity members (promoted points untouched)."""
    await db.execute(
        "DELETE FROM publishing_group_members WHERE group_slug = ? AND member_type = 'entity'",
        (slug,),
    )
    if entity_slugs:
        await db.executemany(
            "INSERT INTO publishing_group_members "
            "(group_slug, entity_slug, member_type) VALUES (?, ?, 'entity')",
            [(slug, s) for s in entity_slugs],
        )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()
    return await get_group_members(db, slug)


async def add_group_point_member(
    db: aiosqlite.Connection,
    slug: str,
    ref: str,
    disp_name: str | None = None,
    disp_unit: str | None = None,
) -> None:
    """Promote a catalog point (``ref`` = "model.point") into a group."""
    await db.execute(
        "INSERT OR REPLACE INTO publishing_group_members "
        "(group_slug, entity_slug, member_type, disp_name, disp_unit) "
        "VALUES (?, ?, 'point', ?, ?)",
        (slug, ref, disp_name, disp_unit),
    )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()


async def remove_group_point_member(db: aiosqlite.Connection, slug: str, ref: str) -> None:
    """Remove a promoted point from a group."""
    await db.execute(
        "DELETE FROM publishing_group_members "
        "WHERE group_slug = ? AND entity_slug = ? AND member_type = 'point'",
        (slug, ref),
    )
    await db.commit()


async def get_group_point_members(db: aiosqlite.Connection, slug: str) -> list[dict]:
    """Return a group's promoted-point members with optional display overrides."""
    db.row_factory = aiosqlite.Row
    try:
        rows: list[dict] = []
        async with db.execute(
            "SELECT entity_slug AS ref, disp_name, disp_unit "
            "FROM publishing_group_members "
            "WHERE group_slug = ? AND member_type = 'point' ORDER BY entity_slug",
            (slug,),
        ) as cursor:
            async for row in cursor:
                rows.append(dict(row))
        return rows
    finally:
        db.row_factory = aiosqlite.Row


async def add_group_member(db: aiosqlite.Connection, slug: str, entity_slug: str) -> None:
    """Add a single entity to a group (idempotent)."""
    await db.execute(
        "INSERT OR IGNORE INTO publishing_group_members (group_slug, entity_slug) VALUES (?, ?)",
        (slug, entity_slug),
    )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()


async def remove_group_member(db: aiosqlite.Connection, slug: str, entity_slug: str) -> None:
    """Remove a single entity from a group."""
    await db.execute(
        "DELETE FROM publishing_group_members WHERE group_slug = ? AND entity_slug = ?",
        (slug, entity_slug),
    )
    await db.execute(
        "UPDATE publishing_groups SET updated_at = ? WHERE slug = ?",
        (time.time(), slug),
    )
    await db.commit()


async def get_disabled_entity_slugs(db: aiosqlite.Connection) -> set[str]:
    """Return curated-entity slugs whose every group is disabled.

    An entity is disabled if every group it belongs to is disabled. Point
    members are excluded (they use the inverse rule — see
    ``get_enabled_promoted_points``).
    """
    slugs: set[str] = set()
    async with db.execute(
        "SELECT m.entity_slug "
        "FROM publishing_group_members m "
        "JOIN publishing_groups g ON g.slug = m.group_slug "
        "WHERE m.member_type = 'entity' "
        "GROUP BY m.entity_slug "
        "HAVING SUM(g.enabled) = 0"
    ) as cursor:
        async for row in cursor:
            slugs.add(row[0])
    return slugs


async def get_enabled_promoted_points(db: aiosqlite.Connection) -> list[dict]:
    """Promoted point refs (model.point) in >=1 ENABLED group, with overrides."""
    db.row_factory = aiosqlite.Row
    try:
        rows: list[dict] = []
        async with db.execute(
            "SELECT m.entity_slug AS ref, "
            "MAX(m.disp_name) AS disp_name, MAX(m.disp_unit) AS disp_unit "
            "FROM publishing_group_members m "
            "JOIN publishing_groups g ON g.slug = m.group_slug "
            "WHERE m.member_type = 'point' "
            "GROUP BY m.entity_slug "
            "HAVING SUM(g.enabled) > 0"
        ) as cursor:
            async for row in cursor:
                rows.append(dict(row))
        return rows
    finally:
        db.row_factory = aiosqlite.Row


async def get_point_catalog_meta(
    db: aiosqlite.Connection, gateway_id: str, model_id: int, point_name: str
) -> dict | None:
    """Catalog metadata (type/unit/label/access) for one model.point, or None."""
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT dp.type, dp.unit, dp.label, dp.access "
            "FROM device_points dp JOIN device_models dm ON dm.id = dp.model_db_id "
            "WHERE dm.gateway_id = ? AND dm.model_id = ? AND dp.point_name = ? LIMIT 1",
            (gateway_id, model_id, point_name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
    finally:
        db.row_factory = aiosqlite.Row


async def get_catalog_points(db: aiosqlite.Connection, gateway_id: str = "default") -> list[dict]:
    """All catalog points for a gateway with metadata + a ``published`` flag."""
    enabled = {p["ref"] for p in await get_enabled_promoted_points(db)}
    db.row_factory = aiosqlite.Row
    try:
        rows: list[dict] = []
        async with db.execute(
            "SELECT dm.model_id, dp.point_name, dp.label, dp.type, dp.unit, "
            "dp.access, dp.addr "
            "FROM device_points dp JOIN device_models dm ON dm.id = dp.model_db_id "
            "WHERE dm.gateway_id = ? ORDER BY dm.model_id, dp.addr",
            (gateway_id,),
        ) as cursor:
            async for row in cursor:
                d = dict(row)
                d["ref"] = f"{d['model_id']}.{d['point_name']}"
                d["published"] = d["ref"] in enabled
                rows.append(d)
        return rows
    finally:
        db.row_factory = aiosqlite.Row
