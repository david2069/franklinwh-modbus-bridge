"""#434: the gateway's connected / last_error / health follow the poller.

``status`` was set only when the gateway started and stopped, and ``health`` by
the 60 s probe, so during a Modbus TCP outage /api/gateways said connected,
no error, for as long as it lasted; after it, *unreachable* for up to a minute.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus
from tests.unit.test_link_state import NOW, _inst


def test_link_down_reads_disconnected_with_the_pollers_error_at_once():
    inst = _inst(connected=False, last_good=NOW - 40)
    inst.status.connected, inst.status.health = True, "connected"
    inst.poller.state.last_error = "Connection error: [Errno 61] Connection refused"
    assert inst.live(now=NOW) == {"connected": False, "health": "unreachable",
                                  "last_error": "Connection error: [Errno 61] Connection refused"}


def test_link_down_with_no_reason_still_says_so():
    inst = _inst(connected=False, last_good=NOW - 40)
    inst.status.connected, inst.status.health = True, "connected"
    assert inst.live(now=NOW)["last_error"] == "Not connected"


def test_a_tcp_only_probe_result_is_kept_while_down():
    inst = _inst(connected=False, last_good=NOW - 40)
    inst.status.connected, inst.status.health = True, "tcp_only"
    assert inst.live(now=NOW)["health"] == "tcp_only"


def test_polls_succeeding_again_read_connected_before_the_next_probe():
    inst = _inst(connected=True, last_good=NOW - 2)
    inst.status.connected, inst.status.health = True, "unreachable"
    assert inst.live(now=NOW) == {"connected": True, "health": "connected", "last_error": None}


def test_a_healthy_gateway_is_unchanged():
    inst = _inst()
    inst.status.connected, inst.status.health = True, "connected"
    assert inst.live(now=NOW) == {"connected": True, "health": "connected", "last_error": None}


def test_a_stopped_gateway_keeps_its_own_status():
    inst = _inst(connected=False, polling=False)
    inst.status.connected, inst.status.health = False, "unknown"
    assert inst.live(now=NOW) == {"connected": False, "health": "unknown", "last_error": None}


def test_to_dict_carries_the_live_values():
    inst = _inst(connected=False, last_good=NOW - 40)
    inst.status.connected, inst.status.health = True, "connected"
    d = inst.to_dict()
    assert d["connected"] is False and d["health"] == "unreachable" and d["last_error"]


async def test_a_failed_reconnect_keeps_the_librarys_reason_and_counts_an_error():
    ctrl = MagicMock()
    ctrl.connect.return_value = False
    ctrl.last_error = "Connection error: [Errno 61] Connection refused"
    stats = MagicMock()
    p = ModbusPoller(controller=ctrl, sample_bus=SampleBus(), gateway_id="gw", poll_interval=0,
                     stats=stats, modbus_lock=asyncio.Lock())
    assert await p._connect() is False
    assert p.state.last_error == "Connection error: [Errno 61] Connection refused"
    assert p.state.errors_total == 1
    stats.record_poll.assert_called_once_with("error")


def test_no_good_read_for_three_polls_reads_disconnected_with_its_age():
    # Socket up, nothing answering: seen on the emulator, a poll took ~2 min to fail.
    inst = _inst(connected=True, last_good=NOW - 45)
    inst.status.connected, inst.status.health = True, "connected"
    assert inst.live(now=NOW) == {"connected": False, "health": "unreachable",
                                  "last_error": "No reply from the aGate for 45 s"}


def test_a_read_within_three_polls_is_still_connected():
    inst = _inst(connected=True, last_good=NOW - 25)
    inst.status.connected, inst.status.health = True, "connected"
    assert inst.live(now=NOW)["connected"] is True
