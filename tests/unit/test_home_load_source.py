"""Home load as franklinwh-modbus returns it; the register used is recorded."""

from __future__ import annotations

import aiosqlite
import pytest

from franklinwh_bridge.modbus.home_load import record_source
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.store.db import create_gateway, get_gateway, run_migrations


def _pts(std, hires, library_choice):
    return {"home_load_ext": library_choice, "home_load_ext_quantized": std, "vreg_16000": hires}


def test_library_value_is_kept_and_16000_recorded():
    p = _pts(900, 929, 929)
    record_source(p)
    assert p["home_load_ext"] == 929 and p["home_load_source"] == "16000"


def test_16000_at_zero_means_15506():
    p = _pts(800, 0, 800)
    record_source(p)
    assert p["home_load_ext"] == 800 and p["home_load_source"] == "15506"


def test_both_zero_is_16000_as_the_library_rules():
    p = _pts(0, 0, 0)
    record_source(p)
    assert p["home_load_source"] == "16000"


def test_no_16000_read_is_15506():
    p = {"home_load_ext": 800, "home_load_ext_quantized": 800}
    record_source(p)
    assert p["home_load_source"] == "15506"


def test_no_extension_read_records_nothing():
    p = {"vreg_16000": 900}
    record_source(p)
    assert "home_load_source" not in p


def test_source_is_a_diagnostic_entity():
    e = {x.slug: x for x in BRIDGE_ENTITIES}["home_load_source"]
    assert e.stat_key == "home_load_source" and e.entity_category == "diagnostic"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


async def _columns(db) -> set[str]:
    async with db.execute("PRAGMA table_info(gateways)") as cur:
        return {row[1] for row in await cur.fetchall()}


async def test_migration_64_drops_the_choice_and_keeps_gateways(db):
    await db.executescript("CREATE TABLE schema_version (version INTEGER, applied_at REAL);")
    from franklinwh_bridge.store import db as dbmod

    all_migrations = dbmod.MIGRATIONS
    try:
        dbmod.MIGRATIONS = {k: v for k, v in all_migrations.items() if k <= 63}
        await run_migrations(db)
        await create_gateway(db, "gw1", "One", "192.0.2.10")
        await db.execute("UPDATE gateways SET home_load_source = 'high_res'")
        await db.commit()
        assert "home_load_source" in await _columns(db)
    finally:
        dbmod.MIGRATIONS = all_migrations
    await run_migrations(db)

    assert "home_load_source" not in await _columns(db)
    gw = await get_gateway(db, "gw1")
    assert gw["name"] == "One" and gw["host"] == "192.0.2.10"


async def test_migration_64_rerun_is_safe(db):
    await run_migrations(await _with_schema_table(db))
    await db.execute("DELETE FROM schema_version WHERE version = 64")
    await db.commit()
    await run_migrations(db)  # column already gone: re-added, then dropped
    assert "home_load_source" not in await _columns(db)


async def _with_schema_table(db):
    await db.executescript("CREATE TABLE schema_version (version INTEGER, applied_at REAL);")
    return db
