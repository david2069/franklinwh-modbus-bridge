"""CommandHandler.state.last_success must reflect the real outcome.

Regression: the command endpoint reported ok=True for any processed command,
so the UI showed a green ✓ even when the hardware rejected the write or the
library lacked the setter. last_success now tracks the true result.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "cmd.db")
    yield conn
    await conn.close()


def _handler(db, ctrl):
    return CommandHandler(ctrl, db, modbus_lock=asyncio.Lock())


async def test_mode_write_success(db):
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(True, "Mode set"))
    h = _handler(db, ctrl)
    await h.handle_command("operating_mode", "Self-Consumption")
    assert h.state.last_success is True


async def test_mode_write_rejected_by_hardware(db):
    # Library ran but the device ignored the write (SPAN lock / read-only).
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(False, "Hardware ignored write"))
    h = _handler(db, ctrl)
    await h.handle_command("operating_mode", "Time of Use")
    assert h.state.last_success is False


_LOCKED = (
    "Write failed (Read-Only?): Hardware ignored write to Self-Consumption. "
    "Register stayed at TOU."
)


async def test_mode_write_ignored_marks_gateway_write_locked(db):
    # #27: the read-back verdict is remembered so the scheduler can skip a
    # release-time mode write that is known to fail.
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(False, _LOCKED))
    h = _handler(db, ctrl)
    assert h.mode_write_locked is False
    await h.handle_command("operating_mode", "Self-Consumption")
    assert h.mode_write_locked is True


async def test_mode_write_other_failure_does_not_mark_locked(db):
    # A timeout or short response proves nothing about the register.
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(False, "Invalid or short response"))
    h = _handler(db, ctrl)
    await h.handle_command("operating_mode", "Self-Consumption")
    assert h.mode_write_locked is False


async def test_mode_write_success_clears_write_locked(db):
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(False, _LOCKED))
    h = _handler(db, ctrl)
    await h.handle_command("operating_mode", "Self-Consumption")
    ctrl.set_native_mode.return_value = (True, "Mode set")
    await h.handle_command("operating_mode", "Self-Consumption")
    assert h.mode_write_locked is False


async def test_mode_method_missing(db):
    ctrl = MagicMock()
    ctrl.set_native_mode = None  # library lacks the setter
    h = _handler(db, ctrl)
    await h.handle_command("operating_mode", "Self-Consumption")
    assert h.state.last_success is False
    assert "not available" in h.state.last_result


async def test_reserve_method_missing(db):
    ctrl = MagicMock()
    ctrl.set_self_consumption_reserve = None  # 0.9.0 case
    h = _handler(db, ctrl)
    await h.handle_command("self_reserve_pct", "40")
    assert h.state.last_success is False
    assert "not available" in h.state.last_result


async def test_reserve_write_success(db):
    ctrl = MagicMock()
    ctrl.set_tou_reserve = MagicMock(return_value=(True, "Reserve set"))
    h = _handler(db, ctrl)
    await h.handle_command("tou_reserve_pct", "55")
    assert h.state.last_success is True
