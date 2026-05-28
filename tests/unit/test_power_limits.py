"""Tests for runtime max power discovery (P4).

Verifies that CommandHandler uses dynamic power limits from M702 nameplate
instead of hardcoded 5000W, and that the MQTT publisher updates entity
discovery payloads accordingly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.publish.command_handler import (
    DEFAULT_MAX_POWER_W,
    CommandHandler,
)
from franklinwh_bridge.publish.entities import get_entity_by_slug
from franklinwh_bridge.publish.mqtt_publisher import (
    DeviceInfo,
    MqttPublisher,
    build_discovery_payload,
)
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest.fixture
def mock_controller():
    ctrl = MagicMock()
    ctrl.read_control_status.return_value = {}
    ctrl.send_command.return_value = (True, "OK")
    ctrl.reset_control_state.return_value = True
    ctrl.get_model.return_value = None
    return ctrl


@pytest.fixture
def handler(mock_controller, db):
    return CommandHandler(mock_controller, db)


@pytest.fixture
def device_info():
    return DeviceInfo(
        serial="10060006A02F00000001",
        manufacturer="FranklinWH",
        model="aGate X",
        firmware="1.0.0",
    )


# ---------------------------------------------------------------------------
# CommandHandler defaults
# ---------------------------------------------------------------------------


def test_default_power_limits(handler):
    """CommandHandler defaults to DEFAULT_MAX_POWER_W."""
    assert handler.max_charge_w == DEFAULT_MAX_POWER_W
    assert handler.max_discharge_w == DEFAULT_MAX_POWER_W
    assert handler.max_power_w == DEFAULT_MAX_POWER_W


def test_default_limits_dict(handler):
    """power_limits reports 'default' source when unset."""
    limits = handler.power_limits
    assert limits["max_charge_w"] == DEFAULT_MAX_POWER_W
    assert limits["max_discharge_w"] == DEFAULT_MAX_POWER_W
    assert limits["source"] == "default"


# ---------------------------------------------------------------------------
# Dynamic limit updates
# ---------------------------------------------------------------------------


def test_set_power_limits(handler):
    """set_power_limits updates the handler's limits."""
    handler.set_power_limits(6000, 5500)
    assert handler.max_charge_w == 6000
    assert handler.max_discharge_w == 5500
    assert handler.max_power_w == 6000
    assert handler.power_limits["source"] == "hardware"


def test_set_asymmetric_limits(handler):
    """Asymmetric charge/discharge limits work correctly."""
    handler.set_power_limits(7000, 3000)
    assert handler.max_charge_w == 7000
    assert handler.max_discharge_w == 3000
    assert handler.max_power_w == 7000  # max of the two


# ---------------------------------------------------------------------------
# Power clamping in commands
# ---------------------------------------------------------------------------


async def test_charge_clamps_to_max_charge(handler):
    """Charge power is clamped to max_charge_w."""
    handler.set_power_limits(3000, 5000)
    handler._command_power_w = 4000  # over charge limit
    await handler._handle_battery_command("Charge")
    # Verify the controller was called with clamped value
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == 3000  # clamped to max_charge


async def test_discharge_clamps_to_max_discharge(handler):
    """Discharge power is clamped to max_discharge_w."""
    handler.set_power_limits(5000, 2500)
    handler._command_power_w = 4000  # over discharge limit
    await handler._handle_battery_command("Discharge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == -2500  # clamped to max_discharge, negative


async def test_charge_under_limit_not_clamped(handler):
    """Power under the limit is not clamped."""
    handler.set_power_limits(6000, 6000)
    handler._command_power_w = 4000  # under limit
    await handler._handle_battery_command("Charge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == 4000  # not clamped


async def test_percentage_uses_directional_max(handler):
    """Percentage mode uses the directional max rate."""
    handler.set_power_limits(6000, 4000)
    handler._command_power_pct = 50
    handler._command_power_w = 0
    await handler._handle_battery_command("Charge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == 3000  # 50% of 6000

    handler._controller.send_command.reset_mock()
    await handler._handle_battery_command("Discharge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == -2000  # 50% of 4000, negative


async def test_default_power_uses_directional_max(handler):
    """When no explicit power is set, default to directional max."""
    handler.set_power_limits(6000, 4000)
    handler._command_power_w = 0
    handler._command_power_pct = 0
    await handler._handle_battery_command("Charge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == 6000  # defaults to max_charge


async def test_input_clamp_uses_symmetric_max(handler):
    """battery_command_power slug clamps to max_power_w (symmetric)."""
    handler.set_power_limits(6000, 4000)
    await handler.handle_command("battery_command_power", "8000")
    assert handler._command_power_w == 6000  # clamped to max(6000, 4000)


# ---------------------------------------------------------------------------
# Max Charge / Max Discharge convenience actions
# ---------------------------------------------------------------------------


async def test_max_charge_uses_max_charge_w(handler):
    """'Max Charge' sends full max_charge_w."""
    handler.set_power_limits(6000, 4000)
    await handler._handle_battery_command("Max Charge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == 6000  # positive = charge


async def test_max_discharge_uses_max_discharge_w(handler):
    """'Max Discharge' sends full max_discharge_w (negative)."""
    handler.set_power_limits(6000, 4000)
    await handler._handle_battery_command("Max Discharge")
    args = handler._controller.send_command.call_args
    cmd = args[0][0]
    assert cmd.power_watts == -4000  # negative = discharge


async def test_max_charge_sets_action_to_charge(handler):
    """'Max Charge' normalises to action='Charge' in state."""
    handler.set_power_limits(5000, 5000)
    await handler._handle_battery_command("Max Charge")
    assert handler._state.action == "Charge"
    assert handler._state.power_w == 5000


async def test_max_discharge_sets_action_to_discharge(handler):
    """'Max Discharge' normalises to action='Discharge' in state."""
    handler.set_power_limits(5000, 5000)
    await handler._handle_battery_command("Max Discharge")
    assert handler._state.action == "Discharge"
    assert handler._state.power_w == 5000


# ---------------------------------------------------------------------------
# Target SoC
# ---------------------------------------------------------------------------


async def test_target_soc_virtual_point(handler):
    """battery_command_target_soc appears in virtual_points."""
    await handler.handle_command("battery_command_target_soc", "80")
    pts = handler.virtual_points
    assert pts["battery_command_target_soc"] == 80


async def test_target_soc_clamped(handler):
    """Target SoC is clamped to 0-100."""
    await handler.handle_command("battery_command_target_soc", "150")
    assert handler._target_soc == 100
    await handler.handle_command("battery_command_target_soc", "-10")
    assert handler._target_soc == 0


async def test_target_soc_zero_disables(handler):
    """Target SoC of 0 disables the feature."""
    await handler.handle_command("battery_command_target_soc", "0")
    assert handler._target_soc == 0
    pts = handler.virtual_points
    assert pts["battery_command_target_soc"] == 0


# ---------------------------------------------------------------------------
# Custom init params
# ---------------------------------------------------------------------------


async def test_init_with_custom_limits(mock_controller, db):
    """CommandHandler accepts initial power limits."""
    h = CommandHandler(
        mock_controller, db,
        max_charge_w=7500, max_discharge_w=5500,
    )
    assert h.max_charge_w == 7500
    assert h.max_discharge_w == 5500
    assert h.power_limits["source"] == "hardware"


# ---------------------------------------------------------------------------
# MQTT publisher max_val override
# ---------------------------------------------------------------------------


def test_power_entity_default_max_val():
    """battery_command_power entity has default max_val=5000."""
    ent = get_entity_by_slug("battery_command_power")
    assert ent is not None
    assert ent.max_val == 5000


def test_publisher_set_power_limits_overrides_max_val():
    """Publisher overrides battery_command_power max_val in discovery."""
    pub = MqttPublisher()
    pub.set_power_limits(7000, 5000)
    assert "battery_command_power" in pub._max_val_overrides
    assert pub._max_val_overrides["battery_command_power"] == 7000


def test_discovery_payload_with_override(device_info):
    """Discovery payload gets overridden max_val when publisher has limits."""
    pub = MqttPublisher()
    pub.set_power_limits(6500, 6500)

    ent = get_entity_by_slug("battery_command_power")
    payload = build_discovery_payload(ent, device_info)

    # Before override is applied (build_discovery_payload uses entity directly)
    assert payload["max"] == 5000  # entity default

    # Override is applied in _publish_discovery, let's simulate:
    if ent.slug in pub._max_val_overrides:
        payload["max"] = pub._max_val_overrides[ent.slug]
    assert payload["max"] == 6500


def test_publisher_set_power_limits_triggers_rediscovery():
    """set_power_limits marks discovery as unpublished."""
    pub = MqttPublisher()
    pub._state.discovery_published = True
    pub.set_power_limits(6000, 6000)
    assert pub._state.discovery_published is False


# ---------------------------------------------------------------------------
# REST endpoint test
# ---------------------------------------------------------------------------


async def test_battery_limits_endpoint(tmp_path, monkeypatch):
    """GET /api/battery/limits returns current power limits."""
    from httpx import ASGITransport, AsyncClient

    from franklinwh_bridge.main import app

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        resp = await client.get("/api/battery/limits")
        assert resp.status_code == 200
        data = resp.json()
        assert "max_charge_w" in data
        assert "max_discharge_w" in data
        assert "source" in data
