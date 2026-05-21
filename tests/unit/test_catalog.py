"""Tests for SunSpec model catalog capture, persistence, and diffing."""

import json
from pathlib import Path

import pytest

from franklinwh_bridge.modbus.catalog import (
    CatalogDiff,
    _compute_diff,
    capture_catalog,
    load_catalog,
    parse_device_info,
)
from franklinwh_bridge.store.db import init_db

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "sunspec_sample.json"


@pytest.fixture
def device_info():
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    await conn.execute(
        "INSERT INTO gateways (id, name, host, port, unit_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("gw1", "Test Gateway", "192.168.1.100", 502, 2, 1000.0),
    )
    await conn.commit()
    yield conn
    await conn.close()


def test_parse_device_info(device_info):
    records = parse_device_info(device_info)
    assert len(records) > 0

    names = {r["point_name"] for r in records}
    assert "Mn" in names
    assert "W" in names
    assert "PVTotal" in names
    assert "ChaState" in names

    writable = [r for r in records if r["writable"]]
    assert len(writable) > 0
    writable_names = {r["point_name"] for r in writable}
    assert "OngridMode" in writable_names
    assert "WChaMax" in writable_names


def test_parse_extracts_model_ids(device_info):
    records = parse_device_info(device_info)
    model_ids = {r["model_id"] for r in records}
    assert model_ids == {1, 101, 124, 64113}


async def test_capture_and_load(device_info, db):
    catalog_hash, diff = await capture_catalog(device_info, db, "gw1")
    assert len(catalog_hash) == 16

    assert diff.added_models == [1, 101, 124, 64113]
    assert diff.removed_models == []
    assert len(diff.added_points) > 0

    loaded = await load_catalog(db, "gw1")
    assert len(loaded) == len(parse_device_info(device_info))

    names = {r["point_name"] for r in loaded}
    assert "PVTotal" in names
    assert "W" in names


async def test_idempotent_recapture(device_info, db):
    h1, d1 = await capture_catalog(device_info, db, "gw1")
    h2, d2 = await capture_catalog(device_info, db, "gw1")

    assert h1 == h2
    assert not d2.has_changes


async def test_diff_detects_added_model(device_info, db):
    await capture_catalog(device_info, db, "gw1")

    modified = json.loads(json.dumps(device_info))
    modified["models"]["999"] = {
        "id": 999,
        "instance": 0,
        "name": "new_model",
        "points": [{"name": "NewPoint", "value": 42, "type": "uint16"}],
    }

    _, diff = await capture_catalog(modified, db, "gw1")
    assert 999 in diff.added_models
    assert "999.NewPoint" in diff.added_points


async def test_diff_detects_removed_model(device_info, db):
    await capture_catalog(device_info, db, "gw1")

    modified = json.loads(json.dumps(device_info))
    del modified["models"]["64113"]

    _, diff = await capture_catalog(modified, db, "gw1")
    assert 64113 in diff.removed_models
    assert any("64113." in p for p in diff.removed_points)


async def test_diff_detects_changed_point(device_info, db):
    await capture_catalog(device_info, db, "gw1")

    modified = json.loads(json.dumps(device_info))
    modified["models"]["124"]["points"][0]["writable"] = False

    _, diff = await capture_catalog(modified, db, "gw1")
    assert "124.WChaMax" in diff.changed_points


def test_compute_diff_empty():
    diff = _compute_diff([], [])
    assert not diff.has_changes


def test_catalog_diff_has_changes():
    diff = CatalogDiff([], [], [], [], [])
    assert not diff.has_changes

    diff = CatalogDiff([1], [], [], [], [])
    assert diff.has_changes
