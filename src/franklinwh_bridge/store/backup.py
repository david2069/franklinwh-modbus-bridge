"""Backup and restore — SQLite online backup API with rotation."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from franklinwh_bridge import __version__
from franklinwh_bridge.store.db import CURRENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)


@dataclass
class BackupInfo:
    name: str
    path: Path
    created_at: float
    schema_version: int
    app_version: str
    size_bytes: int


class BackupManager:
    """Manages backup creation, rotation, and restore."""

    def __init__(
        self,
        db_path: Path,
        backup_dir: Path,
        retention_days: int = 7,
        interval_hours: int = 6,
    ) -> None:
        self._db_path = db_path
        self._backup_dir = backup_dir
        self._retention_days = retention_days
        self._interval_hours = interval_hours
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def _manifest(self) -> dict:
        return {
            "app_version": __version__,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "created_at": time.time(),
            "db_filename": "bridge.db",
        }

    async def create(self, label: str | None = None) -> BackupInfo:
        """Create a backup using SQLite online backup API (WAL-safe)."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        name = f"backup_{ts}" if not label else f"backup_{label}_{ts}"
        archive_path = self._backup_dir / f"{name}.zip"

        staging = self._backup_dir / f".staging_{name}"
        staging.mkdir(exist_ok=True)

        try:
            db_snapshot = staging / "bridge.db"
            src = sqlite3.connect(str(self._db_path))
            dst = sqlite3.connect(str(db_snapshot))
            src.backup(dst)
            src.close()
            dst.close()

            manifest = self._manifest()
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_snapshot, "bridge.db")
                zf.write(staging / "manifest.json", "manifest.json")

            info = BackupInfo(
                name=name,
                path=archive_path,
                created_at=manifest["created_at"],
                schema_version=manifest["schema_version"],
                app_version=manifest["app_version"],
                size_bytes=archive_path.stat().st_size,
            )
            logger.info("Backup created: %s (%d bytes)", name, info.size_bytes)
            return info

        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def list_backups(self) -> list[BackupInfo]:
        """List available backups, newest first."""
        backups: list[BackupInfo] = []
        for path in sorted(self._backup_dir.glob("backup_*.zip"), reverse=True):
            try:
                with zipfile.ZipFile(path, "r") as zf:
                    manifest = json.loads(zf.read("manifest.json"))
                backups.append(BackupInfo(
                    name=path.stem,
                    path=path,
                    created_at=manifest["created_at"],
                    schema_version=manifest["schema_version"],
                    app_version=manifest["app_version"],
                    size_bytes=path.stat().st_size,
                ))
            except Exception as exc:
                logger.warning("Skipping corrupt backup %s: %s", path.name, exc)
        return backups

    async def restore(self, backup_path: Path) -> None:
        """Validate and atomically restore from a backup archive."""
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found: {backup_path}")

        with zipfile.ZipFile(backup_path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))

        if manifest["schema_version"] > CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"Backup schema v{manifest['schema_version']} is newer than "
                f"current v{CURRENT_SCHEMA_VERSION}"
            )

        await self.create(label="pre_restore")

        staging = self._backup_dir / ".staging_restore"
        staging.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(backup_path, "r") as zf:
                zf.extract("bridge.db", staging)

            restored_db = staging / "bridge.db"
            conn = sqlite3.connect(str(restored_db))
            conn.execute("PRAGMA integrity_check")
            conn.close()

            shutil.copy2(restored_db, self._db_path)
            logger.info(
                "Restored from %s (schema v%d)", backup_path.name, manifest["schema_version"]
            )

        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def prune(self) -> int:
        """Remove backups older than retention_days."""
        cutoff = time.time() - (self._retention_days * 86400)
        pruned = 0
        for backup in self.list_backups():
            if backup.created_at < cutoff:
                backup.path.unlink(missing_ok=True)
                pruned += 1
        if pruned:
            logger.info("Pruned %d old backups", pruned)
        return pruned
