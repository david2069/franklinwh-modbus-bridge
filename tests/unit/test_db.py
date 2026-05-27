"""Tests for SQLite store and migrations."""

import pytest

from franklinwh_bridge.store.db import (
    CURRENT_SCHEMA_VERSION,
    get_mqtt_config,
    get_pics_compliance,
    get_schema_version,
    init_db,
    log_startup_event,
    set_pics_status,
)


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await init_db(db_path)
    yield conn
    await conn.close()


async def test_init_creates_db(tmp_path):
    db_path = tmp_path / "test.db"
    assert not db_path.exists()
    conn = await init_db(db_path)
    assert db_path.exists()
    await conn.close()


async def test_schema_version_after_init(db):
    version = await get_schema_version(db)
    assert version == CURRENT_SCHEMA_VERSION


async def test_wal_mode_enabled(db):
    async with db.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
    assert row[0] == "wal"


async def test_foreign_keys_enabled(db):
    async with db.execute("PRAGMA foreign_keys") as cursor:
        row = await cursor.fetchone()
    assert row[0] == 1


async def test_tables_exist(db):
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cursor:
        tables = {row[0] async for row in cursor}
    expected = {
        "schema_version",
        "gateways",
        "gateway_state",
        "device_models",
        "device_points",
        "entity_map",
        "mqtt_config",
        "app_config",
        "metric_samples",
        "startup_log",
        "metrics",
        "operational_stats",
        "metrics_archive",
    }
    assert expected.issubset(tables)


async def test_migrations_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await init_db(db_path)
    v1 = await get_schema_version(conn)
    await conn.close()

    conn = await init_db(db_path)
    v2 = await get_schema_version(conn)
    await conn.close()
    assert v1 == v2 == CURRENT_SCHEMA_VERSION


async def test_startup_log(db):
    await log_startup_event(db, "test_event", "some detail")
    async with db.execute("SELECT event, detail FROM startup_log") as cursor:
        row = await cursor.fetchone()
    assert row[0] == "test_event"
    assert row[1] == "some detail"


async def test_app_config_crud(db):
    await db.execute("INSERT INTO app_config (key, value) VALUES (?, ?)", ("theme", "dark"))
    await db.commit()
    async with db.execute("SELECT value FROM app_config WHERE key = ?", ("theme",)) as cursor:
        row = await cursor.fetchone()
    assert row[0] == "dark"

    await db.execute("UPDATE app_config SET value = ? WHERE key = ?", ("light", "theme"))
    await db.commit()
    async with db.execute("SELECT value FROM app_config WHERE key = ?", ("theme",)) as cursor:
        row = await cursor.fetchone()
    assert row[0] == "light"


async def test_mqtt_config_seeded_by_migration(db):
    config = await get_mqtt_config(db)
    assert config["host"] == "localhost"
    assert config["port"] == 1883
    assert config["enabled"] is True
    assert config["client_id"] == "franklinwh_bridge"
    assert config["topic_prefix"] == "franklinwh"
    assert config["discovery_prefix"] == "homeassistant"


async def test_schema_version_is_7(db):
    version = await get_schema_version(db)
    assert version == 7


async def test_pics_compliance_empty(db):
    rows = await get_pics_compliance(db)
    assert rows == []


async def test_pics_set_and_get(db):
    await set_pics_status(db, 701, "W", "S")
    await set_pics_status(db, 701, "V", "T", notes="reads ok")
    await set_pics_status(db, 702, "CtrlModes", "F", notes="always 0")

    rows = await get_pics_compliance(db)
    assert len(rows) == 3
    assert rows[0]["model_id"] == 701
    assert rows[0]["point_name"] == "V"
    assert rows[0]["status"] == "T"
    assert rows[0]["notes"] == "reads ok"
    assert rows[1]["point_name"] == "W"
    assert rows[1]["status"] == "S"
    assert rows[2]["model_id"] == 702


async def test_pics_upsert(db):
    await set_pics_status(db, 701, "W", "U")
    await set_pics_status(db, 701, "W", "S")
    rows = await get_pics_compliance(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "S"


async def test_pics_invalid_status(db):
    with pytest.raises(ValueError, match="Invalid PICS status"):
        await set_pics_status(db, 701, "W", "Z")
