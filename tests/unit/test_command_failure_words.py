"""#323: a failed battery command says what the error means and that it didn't run.

On aGate V12 a Force Discharge 50 % failed with "Modbus exception: 3". The toast,
the command history and the log said only that, and the log gave "0W" for a %
command. The owner missed what it meant.
"""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish.command_handler import CommandHandler, explain_command_failure
from franklinwh_bridge.store.db import init_db


def test_exception_3_in_words():
    text = explain_command_failure("Modbus exception: 3")
    assert "Rejected by the aGate (Modbus exception 3: illegal data value" in text
    assert "The command did not run; the battery carries on as before." in text


def test_library_form_and_unknown_code():
    assert "exception 2: illegal data address" in explain_command_failure(
        "Modbus exception 2: addr: 70 count: 1")
    assert "doesn't know" in explain_command_failure("Modbus exception: 77")


def test_other_reason_kept_and_previous_command_stopped():
    text = explain_command_failure("Model 704 not available", stopped_previous=True)
    assert text.startswith("Model 704 not available.")
    assert text.endswith("The command did not run, and the previous command has stopped.")


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "cmd.db")
    yield conn
    await conn.close()


async def test_failed_percent_discharge_reads_plainly(db, caplog):
    ctrl = MagicMock()
    ctrl.send_command = MagicMock(return_value=(False, "Modbus exception: 3"))
    ctrl.read_control_status = MagicMock(return_value={"wset_enabled": 0, "wset_pct": 0})
    h = CommandHandler(ctrl, db, modbus_lock=asyncio.Lock())
    await h.handle_command("battery_command_power_pct", "50")
    with caplog.at_level(logging.WARNING):
        await h.handle_command("battery_command", "Force Discharge")
    assert h.state.last_success is False
    assert "illegal data value" in h.state.last_result
    assert "did not run" in h.state.last_result
    assert "Force Discharge 50% — Rejected by the aGate" in caplog.text
