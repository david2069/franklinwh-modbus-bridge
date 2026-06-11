"""Publishing Groups P1: promoted catalog points → entities + membership."""

import pytest

from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, build_discovery_payload
from franklinwh_bridge.publish.promoted_points import point_entity_def, promoted_slug
from franklinwh_bridge.store.db import (
    add_group_point_member,
    create_publishing_group,
    get_group_members,
    get_group_point_members,
    init_db,
    set_group_members,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "pg.db")
    yield conn
    await conn.close()


# ── point_entity_def ────────────────────────────────────────────

def test_point_entity_def_numeric():
    ed = point_entity_def(705, "VRef", dtype="uint16", unit="V", label="Voltage Reference")
    assert ed.slug == "m705_vref"
    assert ed.stat_key == "705.VRef"
    assert ed.source == "705.VRef"
    assert ed.ha_type == "sensor"
    assert ed.unit == "V"
    assert ed.device_class == "voltage"
    assert ed.state_class == "measurement"


def test_point_entity_def_energy_is_total_increasing():
    ed = point_entity_def(502, "OutWh", dtype="acc32", unit="Wh")
    assert ed.device_class == "energy"
    assert ed.state_class == "total_increasing"


def test_point_entity_def_enum_has_no_device_class():
    ed = point_entity_def(713, "Sta", dtype="enum16", unit="")
    assert ed.device_class == ""
    assert ed.unit == ""
    assert ed.state_class == ""


def test_point_entity_def_overrides():
    ed = point_entity_def(705, "VRef", dtype="uint16", unit="V",
                          disp_name="Volt-Var Ref", disp_unit="")
    assert ed.name == "Volt-Var Ref"
    assert ed.unit == ""  # override blanks the unit


def test_promoted_slug_never_collides_with_curated():
    curated = {e.slug for e in BRIDGE_ENTITIES}
    s = promoted_slug(705, "VRef")
    assert s.startswith("m705_")
    assert s not in curated


def test_promoted_point_flows_through_existing_discovery():
    ed = point_entity_def(705, "VRef", dtype="uint16", unit="V", label="Voltage Reference")
    dev = DeviceInfo(serial="10060006A02F00000001", gateway_id="default")
    payload = build_discovery_payload(ed, dev)
    assert payload["unique_id"] == "franklinwh_00000001_m705_vref"
    assert payload["state_topic"] == "franklinwh/00000001/diagnostic/m705_vref"
    assert payload["unit_of_measurement"] == "V"
    assert payload["device_class"] == "voltage"


# ── membership (migration v11) ──────────────────────────────────

async def test_point_members_persist_and_stay_segregated(db):
    await create_publishing_group(db, slug="extra", name="Extra")
    await set_group_members(db, "extra", ["battery_soc", "battery_soh"])
    await add_group_point_member(db, "extra", "705.VRef", disp_name="Volt-Var Ref")
    await add_group_point_member(db, "extra", "713.Sta")

    # Curated-entity members exclude promoted points.
    assert set(await get_group_members(db, "extra")) == {"battery_soc", "battery_soh"}

    # Promoted points are returned separately, with overrides.
    pts = await get_group_point_members(db, "extra")
    assert {p["ref"] for p in pts} == {"705.VRef", "713.Sta"}
    vref = next(p for p in pts if p["ref"] == "705.VRef")
    assert vref["disp_name"] == "Volt-Var Ref"

    # Replacing curated members must NOT wipe promoted points.
    await set_group_members(db, "extra", ["battery_soc"])
    assert await get_group_members(db, "extra") == ["battery_soc"]
    assert len(await get_group_point_members(db, "extra")) == 2


async def _seed_point(db, gateway_id, model_id, point_name, dtype, unit, label):
    from franklinwh_bridge.store.db import create_gateway, get_gateway

    if await get_gateway(db, gateway_id) is None:
        await create_gateway(db, gateway_id, f"GW {gateway_id}", host="1.2.3.4")
    cur = await db.execute(
        "INSERT INTO device_models (gateway_id, model_id, label, captured_at, hash) "
        "VALUES (?, ?, ?, 0, 'h')",
        (gateway_id, model_id, f"Model {model_id}"),
    )
    mdbid = cur.lastrowid
    await db.execute(
        "INSERT INTO device_points (model_db_id, point_name, type, unit, label, addr, access) "
        "VALUES (?, ?, ?, ?, ?, ?, 'R')",
        (mdbid, point_name, dtype, unit, label, 40000),
    )
    await db.commit()


async def test_build_promoted_entities_from_enabled_group(db):
    from franklinwh_bridge.publish.promoted_points import build_promoted_entities
    from franklinwh_bridge.store.db import get_catalog_points

    await _seed_point(db, "default", 705, "VRef", "uint16", "V", "Voltage Reference")
    await create_publishing_group(db, slug="g1", name="G1", enabled=True)
    await add_group_point_member(db, "g1", "705.VRef")

    ents = await build_promoted_entities(db, "default")
    assert len(ents) == 1
    ed = ents[0]
    assert ed.slug == "m705_vref"
    assert ed.stat_key == "705.VRef"
    assert ed.unit == "V"
    assert ed.device_class == "voltage"

    pts = await get_catalog_points(db, "default")
    vref = next(p for p in pts if p["ref"] == "705.VRef")
    assert vref["published"] is True


async def test_promoted_excluded_when_group_disabled(db):
    from franklinwh_bridge.publish.promoted_points import build_promoted_entities

    await _seed_point(db, "default", 705, "VRef", "uint16", "V", "Voltage Reference")
    await create_publishing_group(db, slug="g2", name="G2", enabled=False)
    await add_group_point_member(db, "g2", "705.VRef")

    assert await build_promoted_entities(db, "default") == []
