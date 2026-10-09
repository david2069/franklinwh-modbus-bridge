"""Home load register choice: standard 15506 or high-res 16000 (#35)."""

from __future__ import annotations

import logging

import aiosqlite
import pytest

from franklinwh_bridge.modbus.home_load import HIGH_RES, STANDARD, HomeLoadSelector, plausible
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.store.db import create_gateway, get_gateway, run_migrations


def _pts(std, hires, library_choice=None):
    # The library has already put its own pick (16000 when > 0) in home_load_ext.
    return {
        "home_load_ext": library_choice if library_choice is not None else hires,
        "home_load_ext_quantized": std,
        "vreg_16000": hires,
    }


def test_plausible_follows_the_100w_floor():
    # Correlation evidence (QUIRKS doc): 1004/1000, 929/900, 812/800, 835/800.
    for hires, std in ((1004, 1000), (929, 900), (812, 800), (835, 800)):
        assert plausible(hires, std)
    assert not plausible(1000, 1500)  # guards the reported stuck-at-1000 case (unverified report)


def test_standard_uses_15506_even_though_library_picked_16000():
    p = _pts(900, 929)
    HomeLoadSelector("gw").apply(p, STANDARD)
    assert p["home_load_ext"] == 900
    assert p["home_load_source"] == "15506"


def test_high_res_uses_16000_when_it_agrees():
    p = _pts(900, 929)
    HomeLoadSelector("gw").apply(p, HIGH_RES)
    assert p["home_load_ext"] == 929
    assert p["home_load_source"] == "16000"


def test_high_res_falls_back_when_16000_is_stuck(caplog):
    """The second-site case: 16000 = 1000 while 15506 reads 1500."""
    sel = HomeLoadSelector("gw")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            p = _pts(1500, 1000)
            sel.apply(p, HIGH_RES)
    assert p["home_load_ext"] == 1500
    assert p["home_load_source"] == "15506 (16000 implausible)"
    assert sum("disagrees with 15506" in m for m in caplog.messages) == 1  # once


def test_high_res_recovers_after_agreeing_again():
    sel = HomeLoadSelector("gw")
    sel.apply(_pts(1500, 1000), HIGH_RES)
    p = _pts(1000, 1004)
    sel.apply(p, HIGH_RES)
    assert p["home_load_ext"] == 1004 and p["home_load_source"] == "16000"


def test_failed_extension_read_leaves_points_alone():
    p = {"home_load_ext": 700}
    HomeLoadSelector("gw").apply(p, HIGH_RES)
    assert p == {"home_load_ext": 700}


def test_high_res_without_a_16000_read_uses_15506():
    p = {"home_load_ext_quantized": 800, "home_load_ext": 800}
    HomeLoadSelector("gw").apply(p, HIGH_RES)
    assert p["home_load_ext"] == 800 and p["home_load_source"] == "15506"


def test_source_is_a_diagnostic_entity():
    e = {x.slug: x for x in BRIDGE_ENTITIES}["home_load_source"]
    assert e.stat_key == "home_load_source" and e.entity_category == "diagnostic"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


async def test_migration_keeps_polled_gateways_on_high_res(db):
    """A gateway that has been polled has been showing 16000 — keep it there.
    A never-polled one (no serial) and a mock start on standard."""
    await db.executescript("CREATE TABLE schema_version (version INTEGER, applied_at REAL);")
    # Bring the schema up to v52, add rows, then apply v53.
    from franklinwh_bridge.store import db as dbmod

    all_migrations = dbmod.MIGRATIONS
    try:
        dbmod.MIGRATIONS = {k: v for k, v in all_migrations.items() if k <= 52}
        await run_migrations(db)
        await create_gateway(db, "polled", "Polled", "10.0.0.1")
        await db.execute("UPDATE gateways SET serial = 'X123' WHERE id = 'polled'")
        await create_gateway(db, "fresh", "Fresh", "10.0.0.2")
        await create_gateway(db, "mock1", "Mock", "mock", mock=True)
        await db.execute("UPDATE gateways SET serial = 'MOCK-1' WHERE id = 'mock1'")
        await db.commit()
    finally:
        dbmod.MIGRATIONS = all_migrations
    await run_migrations(db)

    assert (await get_gateway(db, "polled"))["home_load_source"] == "high_res"
    assert (await get_gateway(db, "fresh"))["home_load_source"] == "standard"
    assert (await get_gateway(db, "mock1"))["home_load_source"] == "standard"
