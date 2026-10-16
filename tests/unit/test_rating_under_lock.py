"""The library rating is set under the Modbus lock, just before the send (#230).

franklinwh-modbus <= 0.9.5 clamps an over-limit request in the OPPOSITE
direction (its #28): with a 5000 W rating, +8000 W is sent as -5000 W. The
bridge gives the library the trusted rating and keeps ``watts`` within it, but
it used to set that rating before waiting for the Modbus lock. The poller
reconnects under the same lock, and the library's ``connect()`` rediscovers
ratings, so a reconnect in that gap left the rating below ``watts``: an
over-rating Force Charge went out as a full-rate discharge.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from franklinwh_modbus.controller import FranklinWHController

from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db
from tests.conftest import calibrated_rating

TRUSTED_W = 10000  # e.g. two aPowers, calibrated
DISCOVERED_W = 5000  # what connect() rediscovers: nameplate, or the M702 default


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "r.db")
    yield conn
    await conn.close()


def _controller() -> MagicMock:
    ctrl = MagicMock()
    ctrl.RATED_MAX_CHARGE_W = DISCOVERED_W
    ctrl.RATED_MAX_DISCHARGE_W = DISCOVERED_W
    ctrl.read_control_status.return_value = {}
    ctrl.get_model.return_value = None
    ctrl.sent = []

    def send_command(cmd, duration_s=None):
        # The library's own clamp, run against the rating as it is at send time.
        ctrl.sent.append(FranklinWHController._validate_power(ctrl, cmd.power_watts))
        return True, "OK"

    ctrl.send_command.side_effect = send_command
    return ctrl


@pytest.mark.parametrize(("action", "sign"), [("Force Charge", 1), ("Force Discharge", -1)])
async def test_reconnect_while_waiting_for_the_lock_keeps_the_direction(db, action, sign):
    ctrl = _controller()
    h = CommandHandler(ctrl, db, max_charge_w=TRUSTED_W, max_discharge_w=TRUSTED_W,
                       rating=calibrated_rating(charge_w=TRUSTED_W, discharge_w=TRUSTED_W))
    lock = h._modbus_lock

    await lock.acquire()  # the poller is mid-reconnect
    task = asyncio.create_task(h._handle_battery_command(action))
    await asyncio.sleep(0.05)  # the command is now waiting for the lock
    # connect() -> discover_ratings() resets the library rating
    ctrl.RATED_MAX_CHARGE_W = DISCOVERED_W
    ctrl.RATED_MAX_DISCHARGE_W = DISCOVERED_W
    lock.release()
    await asyncio.wait_for(task, 5)

    assert ctrl.sent == [sign * TRUSTED_W]


async def test_library_clamp_really_inverts_an_over_limit_request():
    """Pins the hazard this guards against, in the installed library. When a
    fixed franklinwh-modbus is adopted (#230) this test should be updated."""
    ctrl = MagicMock(RATED_MAX_CHARGE_W=5000, RATED_MAX_DISCHARGE_W=5000)
    assert FranklinWHController._validate_power(ctrl, 8000) == -5000
