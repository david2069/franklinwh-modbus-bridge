"""#534: aGate V12 refuses every Force Charge over SunSpec Model 704 (exception 3).

The bridge reported "Rejected by the aGate (Modbus exception 3 …)" but never said
why, and the UI and API implied charging works. Now the refusal names the cause,
the API carries battery_charge_refused for the UI's note, and a Force Charge the
aGate accepts (future firmware) clears it. Nothing is blocked.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish.command_handler import CHARGE_REFUSED_NOTE, CommandHandler
from franklinwh_bridge.store.db import init_db
from tests.conftest import calibrated_rating


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "cmd.db")
    yield conn
    await conn.close()


def _handler(db, result):
    ctrl = MagicMock()
    ctrl.send_command = MagicMock(return_value=result)
    ctrl.read_control_status = MagicMock(return_value={"wset_enabled": 0, "wset_pct": 0})
    return ctrl, CommandHandler(ctrl, db, modbus_lock=asyncio.Lock(), rating=calibrated_rating())


async def test_a_refused_charge_names_the_cause(db):
    _, h = _handler(db, (False, "Modbus exception: 3"))
    await h.handle_command("battery_command", "Force Charge")
    assert h.state.last_success is False
    assert "illegal data value" in h.state.last_result
    assert CHARGE_REFUSED_NOTE in h.state.last_result
    assert h.virtual_points["battery_charge_refused"] is True


async def test_a_refused_discharge_or_other_error_does_not(db):
    for action, msg in (("Force Discharge", "Modbus exception: 3"),
                        ("Force Charge", "Modbus exception: 4"),
                        ("Force Charge", "Model 704 not available")):
        _, h = _handler(db, (False, msg))
        await h.handle_command("battery_command", action)
        assert h.state.last_success is False
        assert CHARGE_REFUSED_NOTE not in h.state.last_result
        assert h.virtual_points["battery_charge_refused"] is False


async def test_a_charge_the_agate_accepts_clears_it(db):
    ctrl, h = _handler(db, (False, "Modbus exception: 3"))
    await h.handle_command("battery_command", "Force Charge")
    assert h.virtual_points["battery_charge_refused"] is True
    ctrl.send_command.return_value = (True, "Command Sent: 5000W (WSet=-5000)")
    ctrl.read_control_status.return_value = {"wset_enabled": 1, "wset_mod": 1, "wset_watts": -5000}
    await h.handle_command("battery_command", "Force Charge")
    assert h.state.last_success is True
    assert h.virtual_points["battery_charge_refused"] is False
