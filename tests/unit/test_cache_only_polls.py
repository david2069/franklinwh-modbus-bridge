"""#25: a poll that read nothing from the aGate is a failed poll.

The startup points and the library's model cache filled the sample even when
every read failed, so a dead link polled "ok": SoC stayed at its cached value
as if live, the connectivity monitor flapped outage → "recovered" every ~100 s,
and nothing reconnected. Reproduced on the emulator (no replies on any register,
2026-11-26) before this fix.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus

CACHED = {"713.SoC": 32.0, "713.SoH": 95.0}  # the model cache still holds these after reads stop


@pytest.fixture
def ctrl():
    c = MagicMock()
    c.ip_address = "192.0.2.10"
    c.port = 502
    c.connect.return_value = True
    c.get_model.return_value = None
    for m in ("read_battery_status", "read_grid_status", "read_solar_status",
              "read_control_status", "read_native_mode", "read_alarms"):
        getattr(c, m).return_value = {}  # every read failed (the library returns {})
    return c


@pytest.fixture
def poller(ctrl):
    p = ModbusPoller(controller=ctrl, sample_bus=SampleBus(), gateway_id="gw", poll_interval=0)
    p._startup_points = {"rated_w": 5000}
    p._read_extra_points = lambda: dict(CACHED)
    return p


async def test_a_poll_with_nothing_read_is_an_error_and_shows_no_cached_values(poller):
    sample = await poller._poll_once()
    assert sample.quality == "error"
    assert "soc" not in sample.points
    assert sample.points == {"rated_w": 5000}


async def test_one_value_read_keeps_the_poll_good(poller, ctrl):
    ctrl.read_battery_status.return_value = {"soc": 31.0, "battery_power_w": None}
    sample = await poller._poll_once()
    assert sample.quality == "ok"
    assert sample.points["soc"] == 31.0


async def test_values_none_or_nested_empty_count_as_nothing_read(poller, ctrl):
    ctrl.read_battery_status.return_value = {"soc": None}
    ctrl.read_solar_status.return_value = {"extension": {}}
    assert (await poller._poll_once()).quality == "error"


async def test_a_result_naming_only_the_models_that_failed_is_nothing_read(poller, ctrl):
    # Seen on the emulator with no replies: read_solar_status returned {"partial": ["502", "714"]}.
    ctrl.read_solar_status.return_value = {"partial": ["502", "714"]}
    ctrl.read_battery_status.return_value = {"partial": ["714"]}
    assert (await poller._poll_once()).quality == "error"


async def test_defaults_from_solar_and_alarms_alone_are_nothing_read(poller, ctrl):
    # Seen on the emulator with no usable session: these come back without any read.
    ctrl.read_solar_status.return_value = {"ac_power_w": 0, "dc_power_w": 0, "battery_dc_power_w": 0}
    ctrl.read_alarms.return_value = {"system_alrm": 0, "decoded": {"battery_status": "OK"}}
    assert (await poller._poll_once()).quality == "error"


async def test_the_loop_reconnects_after_a_poll_that_read_nothing(poller, ctrl):
    connects = []
    ctrl.connect.side_effect = lambda: connects.append(1) or True
    ctrl.read_battery_status.return_value = {"soc": 31.0}
    seen = []

    async def on_sample(s):
        seen.append(s.quality)
        ctrl.read_battery_status.return_value = {} if len(seen) == 1 else {"soc": 31.0}
        if len(seen) >= 3:
            poller._stop_event.set()

    poller._bus.subscribe(on_sample)
    poller._backoff_delay = lambda: 0
    await asyncio.wait_for(poller._run_loop(), timeout=10)
    assert seen[:3] == ["ok", "error", "ok"]
    assert len(connects) >= 2  # connected at start, then again after the failed poll
