"""Tests for backup and restore."""

import json
import time
import zipfile

import pytest

from franklinwh_bridge.store.backup import BackupManager
from franklinwh_bridge.store.db import CURRENT_SCHEMA_VERSION, init_db


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "data" / "bridge.db"
    db = await init_db(path)
    await db.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?)", ("theme", "dark")
    )
    await db.commit()
    await db.close()
    return path


@pytest.fixture
def backup_dir(tmp_path):
    d = tmp_path / "data" / "backups"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def manager(db_path, backup_dir):
    return BackupManager(db_path, backup_dir)


async def test_create_backup(manager):
    info = await manager.create()
    assert info.path.exists()
    assert info.size_bytes > 0
    assert info.schema_version == CURRENT_SCHEMA_VERSION
    assert info.name.startswith("backup_")


async def test_create_backup_with_label(manager):
    info = await manager.create(label="test")
    assert "test" in info.name


async def test_list_backups(manager):
    await manager.create(label="a")
    await manager.create(label="b")
    backups = manager.list_backups()
    assert len(backups) == 2
    assert backups[0].created_at >= backups[1].created_at


async def test_backup_contains_manifest(manager):
    info = await manager.create()
    with zipfile.ZipFile(info.path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema_version"] == CURRENT_SCHEMA_VERSION
    assert manifest["db_filename"] == "bridge.db"


async def test_backup_contains_db(manager):
    info = await manager.create()
    with zipfile.ZipFile(info.path, "r") as zf:
        assert "bridge.db" in zf.namelist()


async def test_restore_round_trip(manager, db_path):
    info = await manager.create(label="original")

    db = await init_db(db_path)
    await db.execute("UPDATE app_config SET value = ? WHERE key = ?", ("light", "theme"))
    await db.commit()
    await db.close()

    await manager.restore(info.path)

    db = await init_db(db_path)
    async with db.execute("SELECT value FROM app_config WHERE key = ?", ("theme",)) as cur:
        row = await cur.fetchone()
    await db.close()
    assert row[0] == "dark"


async def test_restore_creates_pre_restore_snapshot(manager):
    await manager.create(label="source")
    backups_before = len(manager.list_backups())

    source = manager.list_backups()[0]
    await manager.restore(source.path)

    backups_after = len(manager.list_backups())
    assert backups_after == backups_before + 1
    pre_restore = [b for b in manager.list_backups() if "pre_restore" in b.name]
    assert len(pre_restore) == 1


async def test_restore_rejects_future_schema(manager, backup_dir):
    info = await manager.create()
    with zipfile.ZipFile(info.path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        db_data = zf.read("bridge.db")

    manifest["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    future_path = backup_dir / "future.zip"
    with zipfile.ZipFile(future_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("bridge.db", db_data)

    with pytest.raises(ValueError, match="newer"):
        await manager.restore(future_path)


async def test_restore_missing_file(manager):
    from pathlib import Path

    with pytest.raises(FileNotFoundError):
        await manager.restore(Path("/nonexistent/backup.zip"))


async def test_prune(manager):
    info = await manager.create(label="old")

    with zipfile.ZipFile(info.path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        db_data = zf.read("bridge.db")

    manifest["created_at"] = time.time() - (8 * 86400)
    with zipfile.ZipFile(info.path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("bridge.db", db_data)

    await manager.create(label="recent")
    pruned = manager.prune()
    assert pruned == 1
    assert len(manager.list_backups()) == 1
