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


# --- AC Type detection ---


def test_ac_type_default(poller):
    """AC type defaults to 0 (single-phase)."""
    assert poller.ac_type == 0


def test_detect_ac_type_single(mock_controller, sample_bus):
    """Detect single-phase ACType=0."""
    m701 = MagicMock()
    m701.ACType = MagicMock()
    m701.ACType.value = 0
    mock_controller.get_model.return_value = m701

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    poller._detect_ac_type()
    assert poller.ac_type == 0


def test_detect_ac_type_split(mock_controller, sample_bus):
    """Detect split-phase ACType=1."""
    m701 = MagicMock()
    m701.ACType = MagicMock()
    m701.ACType.value = 1
    mock_controller.get_model.return_value = m701

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    poller._detect_ac_type()
    assert poller.ac_type == 1


def test_detect_ac_type_three(mock_controller, sample_bus):
    """Detect three-phase ACType=2."""
    m701 = MagicMock()
    m701.ACType = MagicMock()
    m701.ACType.value = 2
    mock_controller.get_model.return_value = m701

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    poller._detect_ac_type()
    assert poller.ac_type == 2


def test_detect_ac_type_no_model(mock_controller, sample_bus):
    """Falls back to single-phase when M701 not available."""
    mock_controller.get_model.return_value = None

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    poller._detect_ac_type()
    assert poller.ac_type == 0


def test_detect_ac_type_corrupt_value(mock_controller, sample_bus):
    """Falls back to single-phase when ACType value is out of range."""
    m701 = MagicMock()
    m701.ACType = MagicMock()
    m701.ACType.value = 99
    mock_controller.get_model.return_value = m701

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    poller._detect_ac_type()
    assert poller.ac_type == 0


# --- Per-phase reading ---


def _make_m701_with_phases(ac_type=0):
    """Create a mock M701 model with per-phase values and scale factors."""
    m701 = MagicMock()
    m701.ACType = MagicMock()
    m701.ACType.value = ac_type

    # Scale factors (typical: -1)
    for sf_name in ("W_SF", "V_SF", "A_SF", "PF_SF", "VA_SF", "Var_SF"):
        sf = MagicMock()
        sf.value = -1
        setattr(m701, sf_name, sf)

    # L1 values (raw unscaled)
    for pt_name, val in [
        ("VL1", 2430),
        ("AL1", 52),
        ("WL1", 12500),
        ("PFL1", 980),
        ("VAL1", 13000),
        ("VarL1", -250),
    ]:
        pt = MagicMock()
        pt.value = val
        setattr(m701, pt_name, pt)

    # L2 values (raw unscaled)
    for pt_name, val in [
        ("VL2", 2410),
        ("AL2", 48),
        ("WL2", 11000),
        ("PFL2", 970),
        ("VAL2", 11500),
        ("VarL2", -200),
        ("VL1L2", 4150),
    ]:
        pt = MagicMock()
        pt.value = val
        setattr(m701, pt_name, pt)

    # L3 values (raw unscaled)
    for pt_name, val in [
        ("VL3", 2420),
        ("AL3", 50),
        ("WL3", 12000),
        ("PFL3", 990),
        ("VAL3", 12500),
        ("VarL3", -230),
        ("VL2L3", 4180),
        ("VL3L1", 4170),
    ]:
        pt = MagicMock()
        pt.value = val
        setattr(m701, pt_name, pt)

    return m701


def test_read_grid_phases_scaling(mock_controller, sample_bus):
    """Per-phase values are correctly scaled by scale factors."""
    m701 = _make_m701_with_phases(ac_type=0)

    def get_model(mid):
        return m701 if mid == 701 else None

    mock_controller.get_model.side_effect = get_model

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    points = poller._read_grid_phases()

    # sf=-1, so raw * 10^(-1) = raw / 10
    assert points["voltage_l1_v"] == 243.0
    assert points["current_l1_a"] == 5.2
    assert points["power_l1_w"] == 1250  # precision=0
    assert points["pf_l1"] == 98.0  # 980 * 0.1, precision=3 → 98.0
    assert points["va_l1"] == 1300
    assert points["var_l1"] == -25

    assert points["ac_type_code"] == 0


def test_read_grid_phases_all_phases(mock_controller, sample_bus):
    """All L1/L2/L3 phase values and line-line voltages are present."""
    m701 = _make_m701_with_phases(ac_type=2)

    def get_model(mid):
        return m701 if mid == 701 else None

    mock_controller.get_model.side_effect = get_model

    poller = ModbusPoller(mock_controller, sample_bus, poll_interval=1)
    points = poller._read_grid_phases()

    # L2 values
    assert "voltage_l2_v" in points
    assert "current_l2_a" in points
    assert "power_l2_w" in points
    assert "voltage_l1l2_v" in points

    # L3 values
    assert "voltage_l3_v" in points
    assert "current_l3_a" in points
    assert "power_l3_w" in points
    assert "voltage_l2l3_v" in points
    assert "voltage_l3l1_v" in points

    assert points["ac_type_code"] == 2


# --- Extension value sanitization (0xFFFF guard) ---


def test_sanitize_replaces_0xffff_with_zero(poller):
    """Individual 0xFFFF register values are replaced with 0 (no previous good value)."""
    points = {
        "total_solar": 0,
        "pv_total": 0,
        "pv_proximal": 65535,
        "pv_remote1": 65535,
        "pv_remote2": 65535,
        "home_load_ext": 350,
    }
    poller._sanitize_extension_values(points)
    assert points["pv_proximal"] == 0
    assert points["pv_remote1"] == 0
    assert points["pv_remote2"] == 0
    assert points["total_solar"] == 0  # recalculated from cleaned components
    assert points["home_load_ext"] == 350  # untouched


def test_sanitize_catches_summed_0xffff(poller):
    """Library-computed total_solar = 196605 (3×65535) is caught and zeroed."""
    points = {
        "total_solar": 196605,  # 65535 * 3
        "pv_total": 0,
        "pv_proximal": 65535,
        "pv_remote1": 65535,
        "pv_remote2": 65535,
        "home_load_ext": 65535,
    }
    poller._sanitize_extension_values(points)
    assert points["total_solar"] == 0
    assert points["pv_proximal"] == 0
    assert points["pv_remote1"] == 0
    assert points["pv_remote2"] == 0
    assert points["home_load_ext"] == 0


def test_sanitize_uses_previous_good_value(poller):
    """Corrupted values are replaced with last-known-good from previous poll."""
    # First poll: normal values
    points1 = {
        "total_solar": 3200,
        "pv_total": 3200,
        "pv_proximal": 3200,
        "pv_remote1": 0,
        "pv_remote2": 0,
        "home_load_ext": 400,
    }
    poller._sanitize_extension_values(points1)
    assert points1["total_solar"] == 3200  # cached as good

    # Second poll: corruption
    points2 = {
        "total_solar": 196605,
        "pv_total": 0,
        "pv_proximal": 65535,
        "pv_remote1": 65535,
        "pv_remote2": 65535,
        "home_load_ext": 65535,
    }
    poller._sanitize_extension_values(points2)
    assert points2["pv_proximal"] == 3200  # previous good value
    assert points2["pv_remote1"] == 0
    assert points2["pv_remote2"] == 0
    assert points2["home_load_ext"] == 400  # previous good value
    # Recalculated total_solar from cleaned components
    assert points2["total_solar"] == 3200


def test_sanitize_rejects_above_max_sane_power(poller):
    """Values above MAX_SANE_POWER_W (15000) are rejected even if not 0xFFFF."""
    points = {
        "total_solar": 20000,
        "pv_total": 20000,
        "pv_proximal": 20000,
        "pv_remote1": 0,
        "pv_remote2": 0,
        "home_load_ext": 500,
    }
    poller._sanitize_extension_values(points)
    assert points["total_solar"] == 0  # no previous good value
    assert points["pv_proximal"] == 0
    assert points["pv_total"] == 0


def test_sanitize_passes_normal_values(poller):
    """Normal production values pass through unchanged."""
    points = {
        "total_solar": 4200,
        "pv_total": 4200,
        "pv_proximal": 4200,
        "pv_remote1": 0,
        "pv_remote2": 0,
        "home_load_ext": 650,
    }
    poller._sanitize_extension_values(points)
    assert points["total_solar"] == 4200
    assert points["pv_proximal"] == 4200
    assert points["home_load_ext"] == 650
