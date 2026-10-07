"""Battery control panel and control log: per-gateway records, target SoC,
and "no time limit" distinguishable from "expired"."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db, load_control_state


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "panel.db")
    yield conn
    await conn.close()


def _controller():
    ctrl = MagicMock()
    ctrl.read_control_status.return_value = {}
    ctrl.send_command.return_value = (True, "OK")
    ctrl.reset_control_state.return_value = True
    ctrl.get_model.return_value = None
    return ctrl


async def _log(db):
    async with db.execute("SELECT event, detail, gateway_id FROM control_log ORDER BY id") as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_log_and_state_are_recorded_against_the_handlers_gateway(db):
    h = CommandHandler(_controller(), db, gateway_id="garage")
    await h.handle_command("battery_command_target_soc", "40")
    await h._handle_battery_command("Force Discharge")

    rows = await _log(db)
    assert rows and all(gw == "garage" for _, _, gw in rows)
    assert ("target_soc_set", "target SoC 40%", "garage") in rows
    sent = next(d for e, d, _ in rows if e == "command_sent")
    assert "no time limit" in sent and "target SoC 40%" in sent

    # Crash-recovery state lands on the garage row, not the primary gateway's.
    assert (await load_control_state(db, "garage")).get("active")
    assert not (await load_control_state(db, "default")).get("active")


async def test_unchanged_target_soc_is_not_logged_again(db):
    h = CommandHandler(_controller(), db)
    await h.handle_command("battery_command_target_soc", "0")  # same as initial
    await h.handle_command("battery_command_target_soc", "55")
    await h.handle_command("battery_command_target_soc", "55")
    events = [e for e, _, _ in await _log(db) if e == "target_soc_set"]
    assert len(events) == 1


async def test_no_time_limit_is_reported_as_a_zero_limit_not_expiry(db):
    h = CommandHandler(_controller(), db)
    await h._handle_battery_command("Force Discharge")
    vp = h.virtual_points
    assert vp["sw_watchdog_limit_s"] == 0 and vp["sw_watchdog_remain_s"] == 0

    h2 = CommandHandler(_controller(), db, gateway_id="other")
    await h2.handle_command("battery_command_duration", "600")
    await h2._handle_battery_command("Force Charge")
    vp = h2.virtual_points
    assert vp["sw_watchdog_limit_s"] == 600 and 0 < vp["sw_watchdog_remain_s"] <= 600


async def test_migration_52_keeps_the_primary_row_and_allows_more(tmp_path):
    """Old single-row table (CHECK id = 1) → one row per gateway, data kept."""
    from franklinwh_bridge.store.db import MIGRATIONS, save_control_state

    db = await init_db(tmp_path / "m52.db")
    try:
        await db.executescript("""
            DROP TABLE control_state;
            CREATE TABLE control_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                active INTEGER NOT NULL DEFAULT 0, action TEXT NOT NULL DEFAULT '',
                power_w INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL DEFAULT 0,
                watchdog_s INTEGER NOT NULL DEFAULT 3600, updated_at REAL NOT NULL DEFAULT 0,
                gateway_id TEXT NOT NULL DEFAULT 'default');
            INSERT INTO control_state (id, active, action, power_w, started_at)
                VALUES (1, 1, 'Force Discharge', 5000, 1700000000);
        """)
        await db.executescript(MIGRATIONS[52])
        primary = await load_control_state(db, "default")
        assert primary["active"] and primary["action"] == "Force Discharge"
        assert primary["power_w"] == 5000

        await save_control_state(db, active=True, action="Force Charge", gateway_id="garage")
        await save_control_state(db, active=False, gateway_id="garage")  # upsert, not a new row
        async with db.execute("SELECT COUNT(*) FROM control_state") as cur:
            assert (await cur.fetchone())[0] == 2
        assert not (await load_control_state(db, "garage"))["active"]
    finally:
        await db.close()
