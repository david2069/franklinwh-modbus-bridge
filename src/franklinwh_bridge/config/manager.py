"""AppConfig -- runtime configuration manager backed by settings + SQLite."""

from __future__ import annotations

from franklinwh_bridge.config.environment import Environment, detect_environment, get_data_dir
from franklinwh_bridge.config.settings import BridgeSettings, load_settings


class AppConfig:
    """Holds resolved runtime configuration for the bridge."""

    def __init__(self, settings: BridgeSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self.environment: Environment = detect_environment()
        self.data_dir = get_data_dir()
        self.db_path = self.data_dir / "bridge.db"
        self.backup_dir = self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
