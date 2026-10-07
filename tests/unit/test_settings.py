"""Tests for settings loading and precedence."""

import json
import os
from unittest.mock import patch

from franklinwh_bridge.config.settings import (
    BridgeSettings,
    GatewaySettings,
    MqttSettings,
    load_settings,
)


def test_default_settings(monkeypatch):
    monkeypatch.delenv("MODBUS_HOST", raising=False)
    monkeypatch.delenv("MODBUS_PORT", raising=False)
    settings = BridgeSettings()
    # No made-up address: an unset host means "not configured".
    assert settings.gateway.host == ""
    assert settings.gateway.port == 502
    assert settings.mqtt.host == "localhost"
    assert settings.mqtt.port == 1883
    assert settings.admin.port == 8099
    assert settings.log_level == "INFO"


def test_env_vars_override_defaults():
    env = {"MODBUS_HOST": "10.0.0.1", "MODBUS_PORT": "503", "MQTT_HOST": "broker.local"}
    with patch.dict(os.environ, env, clear=False):
        gw = GatewaySettings()
        mqtt = MqttSettings()
    assert gw.host == "10.0.0.1"
    assert gw.port == 503
    assert mqtt.host == "broker.local"


def test_load_settings_from_config_file(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
gateway:
  host: "172.16.0.5"
  poll_interval: 60
mqtt:
  host: "mqtt.internal"
  port: 8883
"""
    )
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, {}, clear=True), patch(
        "franklinwh_bridge.config.settings.detect_environment", return_value="dev"
    ):
        settings = load_settings(config)
    assert settings.gateway.host == "172.16.0.5"
    assert settings.gateway.poll_interval == 60
    assert settings.mqtt.host == "mqtt.internal"
    assert settings.mqtt.port == 8883


def test_load_settings_from_json_file(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"gateway": {"host": "10.0.0.99"}}))
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, {}, clear=True), patch(
        "franklinwh_bridge.config.settings.detect_environment", return_value="dev"
    ):
        settings = load_settings(config)
    assert settings.gateway.host == "10.0.0.99"


def test_ha_options_loaded_in_addon_env(tmp_path):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"gateway": {"host": "192.168.50.1"}}))
    with patch.dict(os.environ, {}, clear=True), patch(
        "franklinwh_bridge.config.settings.detect_environment", return_value="ha_addon"
    ), patch("franklinwh_bridge.config.settings._load_ha_options") as mock_ha:
        mock_ha.return_value = {"gateway": {"host": "192.168.50.1"}}
        settings = load_settings()
    assert settings.gateway.host == "192.168.50.1"
