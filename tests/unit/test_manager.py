"""Tests for AppConfig manager."""

from unittest.mock import patch

from franklinwh_bridge.config.manager import AppConfig
from franklinwh_bridge.config.settings import BridgeSettings


def test_app_config_defaults():
    with patch("franklinwh_bridge.config.manager.detect_environment", return_value="dev"), patch(
        "franklinwh_bridge.config.manager.get_data_dir"
    ) as mock_dir:
        from pathlib import Path

        mock_dir.return_value = Path("./data")
        config = AppConfig(settings=BridgeSettings())

    assert config.environment == "dev"
    assert str(config.data_dir) == "data"
    assert config.db_path.name == "bridge.db"
    assert config.backup_dir.name == "backups"


def test_ensure_dirs_creates_directories(tmp_path):
    settings = BridgeSettings()
    with patch("franklinwh_bridge.config.manager.detect_environment", return_value="dev"), patch(
        "franklinwh_bridge.config.manager.get_data_dir", return_value=tmp_path / "data"
    ):
        config = AppConfig(settings=settings)
    config.ensure_dirs()
    assert config.data_dir.exists()
    assert config.backup_dir.exists()
