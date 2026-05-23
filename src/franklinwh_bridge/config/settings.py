"""Pydantic settings -- env vars > config file > HA options.json > defaults."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from franklinwh_bridge.config.environment import detect_environment


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODBUS_")

    host: str = "192.168.1.100"
    port: int = 502
    unit_id: int = 1
    poll_interval: int = 10


class MqttSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MQTT_")

    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADMIN_")

    port: int = 8099
    username: str | None = None
    password: str | None = None


class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BACKUP_")

    enabled: bool = True
    interval_hours: int = 6
    retention_days: int = 7


class BridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    app_env: str | None = Field(default=None, alias="APP_ENV")
    log_level: str = "INFO"

    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    mqtt: MqttSettings = Field(default_factory=MqttSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)


def _load_ha_options() -> dict:
    """Load HA addon options.json if present."""
    options_path = Path("/data/options.json")
    if options_path.exists():
        return json.loads(options_path.read_text())
    return {}


def _load_config_file(path: Path | None = None) -> dict:
    """Load YAML or JSON config file if present."""
    if path and path.exists():
        import yaml  # deferred import; only needed when config file exists

        return yaml.safe_load(path.read_text()) or {}

    for candidate in [Path("config.yaml"), Path("config.json")]:
        if candidate.exists():
            if candidate.suffix == ".json":
                return json.loads(candidate.read_text())
            import yaml

            return yaml.safe_load(candidate.read_text()) or {}
    return {}


def load_settings(config_path: Path | None = None) -> BridgeSettings:
    """Load settings with precedence: env vars > config file > HA options > defaults."""
    env = detect_environment()

    file_config = _load_config_file(config_path)
    ha_options = _load_ha_options() if env == "ha_addon" else {}

    merged: dict = {}
    for source in [ha_options, file_config]:
        for key, value in source.items():
            if isinstance(value, dict):
                merged.setdefault(key, {}).update(value)
            else:
                merged[key] = value

    gateway_kwargs = merged.get("gateway", {})
    mqtt_kwargs = merged.get("mqtt", {})
    admin_kwargs = merged.get("admin", {})
    backup_kwargs = merged.get("backup", {})

    return BridgeSettings(
        gateway=GatewaySettings(**gateway_kwargs),
        mqtt=MqttSettings(**mqtt_kwargs),
        admin=AdminSettings(**admin_kwargs),
        backup=BackupSettings(**backup_kwargs),
        log_level=merged.get("logging", {}).get("level", "INFO"),
    )
