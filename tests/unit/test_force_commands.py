"""Force-prefixed battery dispatch vocabulary + superseded-dispatch audit.

Covers the "Force Charge" / "Force Discharge" / "Force Standby" labels that
distinguish a user-commanded dispatch from the device's normal charge/
discharge, plus the ``command_superseded`` audit event written when a new
dispatch replaces a different active one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "force.db")
    yield conn
    await conn.close()


@pytest.fixture
def mock_controller():
    ctrl = MagicMock()
    ctrl.read_control_status.return_value = {}
    ctrl.send_command.return_value = (True, "OK")
    ctrl.reset_control_state.return_value = True
    ctrl.get_model.return_value = None  # no M704 → revert-timer write is a no-op
    return ctrl


@pytest.fixture
def handler(mock_controller, db):
    return CommandHandler(mock_controller, db, max_charge_w=5000, max_discharge_w=5000)


async def _control_events(db) -> list[tuple[str, str]]:
    """Return (event, action) rows from control_log, oldest first."""
    rows = []
    async with db.execute(
        "SELECT event, action FROM control_log ORDER BY id"
    ) as cur:
        async for r in cur:
            rows.append((r[0], r[1]))
    return rows


# ── canonical Force labels ──────────────────────────────────────

async def test_force_charge_sets_prefixed_state(handler):
    await handler._handle_battery_command("Force Charge")
    assert handler.state.active is True
    assert handler.state.action == "Force Charge"
    assert handler.virtual_points["battery_command_state"] == "Force Charge"
    # Charge → controller commanded with positive watts
    cmd = handler._controller.send_command.call_args.args[0]
    assert cmd.power_watts > 0


async def test_force_discharge_is_negative_watts(handler):
    await handler._handle_battery_command("Force Discharge")
    assert handler.state.action == "Force Discharge"
    cmd = handler._controller.send_command.call_args.args[0]
    assert cmd.power_watts < 0


async def test_force_standby_is_zero_watts(handler):
    await handler._handle_battery_command("Force Standby")
    assert handler.state.action == "Force Standby"
    cmd = handler._controller.send_command.call_args.args[0]
    assert cmd.power_watts == 0


async def test_legacy_bare_verb_normalizes_to_force(handler):
    """Legacy/retained 'Charge' payloads still work, normalised to the
    canonical 'Force Charge' label."""
    await handler._handle_battery_command("Charge")
    assert handler.state.action == "Force Charge"


async def test_release_clears_state(handler):
    await handler._handle_battery_command("Force Charge")
    await handler._handle_battery_command("Release")
    assert handler.state.active is False
    assert handler.state.action == ""
    assert handler.virtual_points["battery_command_state"] == "Not Active"


# ── superseded-dispatch audit ───────────────────────────────────

async def test_superseded_dispatch_is_logged(handler, db):
    await handler._handle_battery_command("Force Charge")
    await handler._handle_battery_command("Force Discharge")  # supersede

    events = await _control_events(db)
    superseded = [a for (e, a) in events if e == "command_superseded"]
    # The Charge dispatch should be recorded as superseded by the Discharge.
    assert superseded == ["Force Charge"]
    assert handler.state.action == "Force Discharge"


async def test_power_tweak_does_not_log_supersede(handler, db):
    """Re-applying the same action (e.g. a power change) is not a supersede."""
    await handler._handle_battery_command("Force Charge")
    await handler.handle_command("battery_command_power", "2000")  # re-applies

    events = await _control_events(db)
    assert not [e for (e, _a) in events if e == "command_superseded"]
