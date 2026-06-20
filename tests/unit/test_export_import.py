"""Tests for metrics export/import and backup REST API."""

from __future__ import annotations

import csv
import io
import json
import time

import pytest

from franklinwh_bridge.store.db import init_db
from franklinwh_bridge.store.metrics import (
    export_metrics,
    format_metrics_csv,
    import_metrics_csv,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# Metrics export tests
# ---------------------------------------------------------------------------


async def test_export_empty(db):
    """Export with no data returns empty list."""
    rows = await export_metrics(db, range_seconds=3600)
    assert rows == []


async def test_export_returns_rows(db):
    """Export returns inserted metrics with ISO timestamps."""
    now = time.time()
    for i in range(5):
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now - 300 + i * 60, 100 + i, 200, 300, 400, 50 + i),
        )
    await db.commit()

    rows = await export_metrics(db, range_seconds=3600)
    assert len(rows) == 5
    # Rows should have ISO timestamps
    assert "T" in rows[0]["timestamp"]
    assert rows[0]["battery_w"] == 100
    assert rows[-1]["soc"] == 54


async def test_export_sorted_by_time(db):
    """Export results are sorted oldest-first."""
    now = time.time()
    # Insert out of order
    for ts in [now - 100, now - 300, now - 200]:
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, 100, 200, 300, 400, 50)",
            (ts,),
        )
    await db.commit()

    rows = await export_metrics(db, range_seconds=3600)
    assert len(rows) == 3
    # Timestamps should be ascending
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps)


async def test_export_includes_archive(db):
    """Export unions raw + archive data for long ranges."""
    now = time.time()
    # Insert raw recent data
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, 100, 200, 300, 400, 50)",
        (now - 3600,),
    )
    # Insert archive data (>7 days old)
    old_ts = now - 10 * 86400
    await db.execute(
        "INSERT INTO metrics_archive "
        "(ts, battery_w, grid_w, solar_w, home_w, soc, sample_count) "
        "VALUES (?, 500, 600, 700, 800, 90, 10)",
        (old_ts,),
    )
    await db.commit()

    rows = await export_metrics(db, range_seconds=30 * 86400)
    assert len(rows) == 2
    # Archive row should come first (older)
    assert rows[0]["battery_w"] == 500


async def test_export_respects_range(db):
    """Export only returns data within the requested range."""
    now = time.time()
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, 100, 200, 300, 400, 50)",
        (now - 60,),
    )
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, 999, 999, 999, 999, 99)",
        (now - 7200,),  # 2h ago
    )
    await db.commit()

    rows = await export_metrics(db, range_seconds=3600)
    assert len(rows) == 1
    assert rows[0]["battery_w"] == 100


# ---------------------------------------------------------------------------
# CSV formatting tests
# ---------------------------------------------------------------------------


def test_format_csv_headers():
    """CSV output includes correct headers."""
    rows = [
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "battery_w": 100,
            "grid_w": 200,
            "solar_w": 300,
            "home_w": 400,
            "soc": 50,
        }
    ]
    csv_text = format_metrics_csv(rows)
    reader = csv.DictReader(io.StringIO(csv_text))
    assert reader.fieldnames == [
        "timestamp", "battery_w", "grid_w", "solar_w", "home_w", "soc",
        "ambient_temp_c", "cabinet_temp_c", "mode_name", "self_reserve_pct", "tou_reserve_pct", "grid_mode",
    ]
    parsed = list(reader)
    assert len(parsed) == 1
    assert parsed[0]["battery_w"] == "100"


def test_format_csv_empty():
    """CSV with no rows has only header line."""
    csv_text = format_metrics_csv([])
    lines = csv_text.strip().split("\n")
    assert len(lines) == 1
    assert "timestamp" in lines[0]


# ---------------------------------------------------------------------------
# Metrics import tests
# ---------------------------------------------------------------------------


async def test_import_basic(db):
    """Import valid CSV rows into metrics table."""
    csv_text = (
        "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
        "1700000000,100,200,300,400,50\n"
        "1700000060,110,210,310,410,51\n"
    )
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 2

    async with db.execute("SELECT COUNT(*) FROM metrics") as cur:
        count = (await cur.fetchone())[0]
    assert count == 2


async def test_import_iso_timestamps(db):
    """Import with ISO-8601 timestamps."""
    csv_text = (
        "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
        "2025-01-01T00:00:00+00:00,100,200,300,400,50\n"
        "2025-01-01T00:01:00+00:00,110,210,310,410,51\n"
    )
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 2


async def test_import_skips_empty_rows(db):
    """Import skips rows where all values are empty."""
    csv_text = (
        "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
        "1700000000,100,200,300,400,50\n"
        "1700000060,,,,, \n"
    )
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 1


async def test_import_skips_bad_timestamps(db):
    """Import skips rows with unparseable timestamps."""
    csv_text = (
        "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
        "not-a-timestamp,100,200,300,400,50\n"
        "1700000060,110,210,310,410,51\n"
    )
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 1


async def test_import_handles_partial_values(db):
    """Import accepts rows with some missing columns."""
    csv_text = (
        "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
        "1700000000,100,,300,,50\n"
    )
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 1

    async with db.execute(
        "SELECT battery_w, grid_w, solar_w, home_w, soc FROM metrics"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 100
    assert row[1] is None
    assert row[2] == 300
    assert row[3] is None
    assert row[4] == 50


async def test_import_returns_zero_on_empty(db):
    """Import with no data rows returns 0."""
    csv_text = "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 0


# ---------------------------------------------------------------------------
# Round-trip export → import test
# ---------------------------------------------------------------------------


async def test_export_import_round_trip(db):
    """Data survives export → import cycle."""
    now = time.time()
    for i in range(10):
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now - 600 + i * 60, 100 + i, 200 + i, 300 + i, 400 + i, 50 + i),
        )
    await db.commit()

    # Export
    rows = await export_metrics(db, range_seconds=3600)
    csv_text = format_metrics_csv(rows)

    # Delete originals
    await db.execute("DELETE FROM metrics")
    await db.commit()

    # Re-import
    imported = await import_metrics_csv(db, csv_text)
    assert imported == 10

    # Verify data survived
    async with db.execute(
        "SELECT battery_w, soc FROM metrics ORDER BY ts"
    ) as cur:
        result = await cur.fetchall()
    assert (result[0]["battery_w"], result[0]["soc"]) == (100.0, 50.0)
    assert (result[-1]["battery_w"], result[-1]["soc"]) == (109.0, 59.0)


# ---------------------------------------------------------------------------
# Backup REST API tests (via ASGI test client)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from franklinwh_bridge.main import app

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_backup_create(client):
    """POST /api/backup/create returns backup metadata."""
    resp = await client.post("/api/backup/create", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "name" in data
    assert data["name"].startswith("backup_")
    assert data["size_bytes"] > 0
    assert data["schema_version"] > 0


async def test_backup_create_with_label(client):
    """POST /api/backup/create with label includes it in name."""
    resp = await client.post(
        "/api/backup/create", json={"label": "test_label"}
    )
    assert resp.status_code == 200
    assert "test_label" in resp.json()["name"]


async def test_backup_list(client):
    """GET /api/backup/list returns created backups."""
    await client.post("/api/backup/create", json={"label": "a"})
    await client.post("/api/backup/create", json={"label": "b"})

    resp = await client.get("/api/backup/list")
    assert resp.status_code == 200
    backups = resp.json()["backups"]
    assert len(backups) >= 2
    # Newest first
    assert backups[0]["created_at"] >= backups[1]["created_at"]


async def test_backup_download(client):
    """GET /api/backup/download/{name} returns ZIP file."""
    create_resp = await client.post("/api/backup/create", json={})
    name = create_resp.json()["name"]

    resp = await client.get(f"/api/backup/download/{name}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    # Should be a valid ZIP (starts with PK signature)
    assert resp.content[:2] == b"PK"


async def test_backup_download_not_found(client):
    """GET /api/backup/download/{name} for missing backup returns 404."""
    resp = await client.get("/api/backup/download/nonexistent")
    assert resp.status_code == 404


async def test_backup_restore(client):
    """POST /api/backup/restore round-trips data."""
    # Set a config value
    await client.put("/api/config/test_key", json={"value": "original"})

    # Create backup
    create_resp = await client.post("/api/backup/create", json={})
    name = create_resp.json()["name"]

    # Change the value
    await client.put("/api/config/test_key", json={"value": "modified"})

    # Restore
    resp = await client.post(
        "/api/backup/restore", json={"name": name}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_backup_restore_not_found(client):
    """POST /api/backup/restore with missing name returns 404."""
    resp = await client.post(
        "/api/backup/restore", json={"name": "nonexistent"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Metrics export/import REST API tests
# ---------------------------------------------------------------------------


async def test_metrics_export_csv(client):
    """GET /api/metrics/export?format=csv returns CSV."""
    from franklinwh_bridge.main import app

    db = app.state.db
    now = time.time()
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, 100, 200, 300, 400, 50)",
        (now - 60,),
    )
    await db.commit()

    resp = await client.get("/api/metrics/export?format=csv&range=1h")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "timestamp" in resp.text
    assert "battery_w" in resp.text


async def test_metrics_export_json(client):
    """GET /api/metrics/export?format=json returns JSON."""
    from franklinwh_bridge.main import app

    db = app.state.db
    now = time.time()
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, 100, 200, 300, 400, 50)",
        (now - 60,),
    )
    await db.commit()

    resp = await client.get("/api/metrics/export?format=json&range=1h")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert "rows" in data
    assert data["count"] >= 1


async def test_metrics_export_invalid_range(client):
    """GET /api/metrics/export with invalid range returns 400."""
    resp = await client.get("/api/metrics/export?range=99h")
    assert resp.status_code == 400


async def test_metrics_export_invalid_format(client):
    """GET /api/metrics/export with invalid format returns 400."""
    resp = await client.get("/api/metrics/export?format=xml")
    assert resp.status_code == 400


async def test_metrics_import_csv(client):
    """POST /api/metrics/import uploads CSV data."""
    csv_text = (
        "timestamp,battery_w,grid_w,solar_w,home_w,soc\n"
        "1700000000,100,200,300,400,50\n"
        "1700000060,110,210,310,410,51\n"
    )
    resp = await client.post(
        "/api/metrics/import",
        files={"file": ("metrics.csv", csv_text, "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["imported_rows"] == 2


async def test_metrics_import_rejects_non_csv(client):
    """POST /api/metrics/import rejects non-CSV files."""
    resp = await client.post(
        "/api/metrics/import",
        files={"file": ("data.txt", "hello", "text/plain")},
    )
    assert resp.status_code == 400
