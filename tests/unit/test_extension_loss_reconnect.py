"""#317: the 15500-15509 block going missing forces a full reconnect.

A failed library-internal reconnect() leaves controller.dev None while the
cached SunSpec models go on reading, so only the extension block stops
(franklinwh-modbus#89). The poll looked healthy and nothing reconnected: solar,
home, mode and reserves were missing for 3.5 h on a live aGate.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.main import LIBRARY_LOGGER, configure_console_logging
from franklinwh_bridge.modbus.poller import EXTENSION_MISSING_RECONNECT_POLLS, ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus

EXT = {"extension": {"ongrid_mode": 2, "total_solar": 3800, "self_reserve": 8}}


@pytest.fixture
def ctrl():
    c = MagicMock()
    c.ip_address = "192.0.2.10"
    c.port = 502
    c.connect.return_value = True
    c.read_battery_status.return_value = {"soc": 99}
    c.read_solar_status.return_value = dict(EXT)
    c.get_model.return_value = None
    return c


@pytest.fixture
def poller(ctrl):
    return ModbusPoller(controller=ctrl, sample_bus=SampleBus(), gateway_id="gw", poll_interval=0)


def test_healthy_polls_never_ask_for_a_reconnect(poller):
    for _ in range(10):
        assert poller._library_session_lost({"ongrid_mode": 2}) is None


def test_lost_library_device_is_caught_at_once(poller, ctrl):
    ctrl.dev = None
    assert "lost its device" in poller._library_session_lost({"ongrid_mode": 2})


def test_missing_block_reconnects_after_n_polls(poller):
    assert poller._library_session_lost({"ongrid_mode": 2}) is None
    for _ in range(EXTENSION_MISSING_RECONNECT_POLLS - 1):
        assert poller._library_session_lost({"soc": 99}) is None
    assert "15500-15509" in poller._library_session_lost({"soc": 99})
    # The count starts again after asking, so a reconnect isn't asked for every poll.
    assert poller._library_session_lost({"soc": 99}) is None


def test_a_gateway_that_never_had_the_block_is_left_alone(poller):
    for _ in range(EXTENSION_MISSING_RECONNECT_POLLS * 3):
        assert poller._library_session_lost({"soc": 99}) is None


async def test_poll_loop_reconnects_and_recovers(poller, ctrl, caplog):
    connects = []

    def connect():
        connects.append(1)
        ctrl.read_solar_status.return_value = dict(EXT)  # a fresh session reads it again
        return True

    ctrl.connect.side_effect = connect
    seen = []

    async def on_sample(s):
        seen.append("ongrid_mode" in s.points)
        if len(seen) == 1:
            ctrl.read_solar_status.return_value = {}  # the block stops arriving
        if len(seen) >= EXTENSION_MISSING_RECONNECT_POLLS + 3:
            poller._stop_event.set()

    poller._bus.subscribe(on_sample)
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(poller._run_loop(), 5)

    assert len(connects) == 2  # start-up, then the forced reconnect
    assert seen[-1] is True
    assert "15500-15509" in caplog.text and "reconnecting" in caplog.text


def test_library_warnings_reach_the_bridge_console():
    configure_console_logging(logging.INFO)
    configure_console_logging(logging.INFO)  # idempotent
    lib = logging.getLogger(LIBRARY_LOGGER)
    consoles = [h for h in lib.handlers if getattr(h, "_bridge_console", False)]
    assert len(consoles) == 1
    assert lib.isEnabledFor(logging.WARNING) and not lib.isEnabledFor(logging.INFO)
