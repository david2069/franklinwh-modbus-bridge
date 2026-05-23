"""Tests for SQLite store and migrations."""

import pytest

from franklinwh_bridge.store.db import (
    CURRENT_SCHEMA_VERSION,
    get_mqtt_config,
    get_schema_version,
    init_db,
    log_startup_event,
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


async def test_schema_version_is_4(db):
    version = await get_schema_version(db)
    assert version == 4
