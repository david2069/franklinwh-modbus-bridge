"""Tests for the Modbus poller with a mocked controller."""

import asyncio
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus


@pytest.fixture
def mock_controller():
    ctrl = MagicMock()
    ctrl.ip_address = "192.168.1.100"
    ctrl.port = 502
    ctrl.unit_id = 1
    ctrl.connect.return_value = True
    ctrl.disconnect.return_value = None
    ctrl.read_battery_status.return_value = {"soc": 85, "power": -1200}
    ctrl.read_grid_status.return_value = {"grid_power": 500, "grid_voltage": 240.1}
    ctrl.read_solar_status.return_value = {"pv_power": 3200}
    ctrl.read_nameplate.return_value = {"rated_power": 5000}
    ctrl.read_control_status.return_value = {"mode": "self_consumption"}
    ctrl.read_native_mode.return_value = {"native_mode": 2}
    ctrl.read_alarms.return_value = {"active_alarms": []}
    ctrl.get_model.return_value = None
    return ctrl


@pytest.fixture
def sample_bus():
    return SampleBus()


@pytest.fixture
def poller(mock_controller, sample_bus):
    return ModbusPoller(
        controller=mock_controller,
        sample_bus=sample_bus,
        gateway_id="test_gw",
        poll_interval=1,
    )


async def test_poll_once_merges_all_reads(poller, sample_bus):
    sample = await poller.poll_once()
    assert sample.gateway_id == "test_gw"
    assert sample.quality == "ok"
    assert sample.points["soc"] == 85
    assert sample.points["grid_power"] == 500
    assert sample.points["pv_power"] == 3200
    assert sample.points["rated_power"] == 5000


async def test_poll_once_publishes_to_bus(poller, sample_bus):
    received = []

    async def on_sample(s):
        received.append(s)

    sample_bus.subscribe(on_sample)
    await poller.poll_once()
    assert len(received) == 1


async def test_poller_state_updates(poller):
    assert poller.state.polls_total == 0
    await poller.poll_once()
    assert poller.state.polls_total == 1
    assert poller.state.last_poll_ts is not None
    assert poller.state.connected is True


async def test_poller_handles_partial_failure(mock_controller, sample_bus):
    mock_controller.read_battery_status.side_effect = Exception("timeout")
    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    sample = await poller.poll_once()
    assert sample.quality == "stale"
    assert "grid_power" in sample.points
    assert "soc" not in sample.points


async def test_poller_handles_total_failure(mock_controller, sample_bus):
    for method_name in [
        "read_battery_status",
        "read_grid_status",
        "read_solar_status",
        "read_nameplate",
        "read_control_status",
        "read_native_mode",
        "read_alarms",
    ]:
        getattr(mock_controller, method_name).side_effect = Exception("down")

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    sample = await poller.poll_once()
    assert sample.quality == "error"
    assert len(sample.points) == 0


async def test_poller_connect_failure_sets_state(mock_controller, sample_bus):
    mock_controller.connect.side_effect = ConnectionError("refused")
    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    connected = await poller._connect()
    assert connected is False
    assert poller.state.connected is False
    assert "refused" in poller.state.last_error


async def test_poller_backoff_grows():
    poller = ModbusPoller(MagicMock(), SampleBus(), poll_interval=1)
    poller._state.consecutive_errors = 0
    assert poller._backoff_delay() == 5.0
    poller._state.consecutive_errors = 3
    assert poller._backoff_delay() == 40.0
    poller._state.consecutive_errors = 10
    assert poller._backoff_delay() == 60.0


async def test_poller_start_stop(poller):
    await poller.start()
    assert poller._task is not None
    assert not poller._task.done()
    await asyncio.sleep(0.05)
    await poller.stop()
    assert poller._task.done()
