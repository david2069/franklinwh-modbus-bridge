"""A timed force is released by the bridge's watchdog, not the library's timer (#242).

franklinwh-modbus runs ``send_command(duration_s=...)``'s timeout on a
``threading.Timer`` that calls ``reset_control_state()`` without the bridge's
Modbus lock, so its writes could interleave with a poll, and the command was
released twice (once unaudited).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish import command_handler as ch
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db
from tests.conftest import calibrated_rating


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "t.db")
    yield conn
    await conn.close()


def _controller() -> MagicMock:
    ctrl = MagicMock()
    ctrl.read_control_status.return_value = {}
    ctrl.get_model.return_value = None
    ctrl.send_command.return_value = (True, "OK")
    ctrl.reset_control_state.return_value = True
    return ctrl


async def test_timed_force_gives_the_library_no_duration(db):
    ctrl = _controller()
    h = CommandHandler(ctrl, db, max_charge_w=5000, max_discharge_w=5000,
                       rating=calibrated_rating())
    await h.handle_command("battery_command_duration", "600")
    await h.handle_command("battery_command", "Force Charge")
    assert h.state.active and h.state.watchdog_s == 600
    _, kwargs = ctrl.send_command.call_args
    assert kwargs.get("duration_s") is None


async def test_watchdog_releases_the_timed_force_once(db, monkeypatch):
    monkeypatch.setattr(ch, "SOC_CHECK_INTERVAL_S", 0.01)
    ctrl = _controller()
    h = CommandHandler(ctrl, db, max_charge_w=5000, max_discharge_w=5000,
                       rating=calibrated_rating())
    await h.handle_command("battery_command_duration", "1")
    await h.handle_command("battery_command", "Force Discharge")
    h._state.started_at -= 5  # the 1 s limit has passed
    await h._watchdog_task
    assert not h.state.active
    assert ctrl.reset_control_state.call_count == 1
