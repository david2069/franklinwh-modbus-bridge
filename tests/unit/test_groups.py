"""Tests for publishing groups store and API."""

import pytest

from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES
from franklinwh_bridge.store.db import (
    add_group_member,
    create_publishing_group,
    delete_publishing_group,
    get_disabled_entity_slugs,
    get_group_members,
    get_publishing_group,
    get_publishing_groups,
    init_db,
    remove_group_member,
    set_group_members,
    update_publishing_group,
)


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await init_db(db_path)
    yield conn
    await conn.close()


# ── Seed & schema ──────────────────────────────────────────────


async def test_default_groups_seeded(db):
    groups = await get_publishing_groups(db)
    assert len(groups) == 7
    slugs = [g["slug"] for g in groups]
    assert slugs == ["battery", "capacity", "power", "energy", "solar", "status", "control"]


async def test_all_entities_mapped(db):
    """Every entity in BRIDGE_ENTITIES should be in exactly one default group."""
    groups = await get_publishing_groups(db)
    total = sum(g["member_count"] for g in groups)
    assert total == len(BRIDGE_ENTITIES)


async def test_default_groups_are_enabled(db):
    groups = await get_publishing_groups(db)
    for g in groups:
        assert g["enabled"] is True
        assert g["is_default"] is True


async def test_battery_group_members(db):
    grp = await get_publishing_group(db, "battery")
    assert grp is not None
    assert sorted(grp["members"]) == [
        "battery_current_a", "battery_health", "battery_power_kw",
        "battery_soc", "battery_soh", "battery_state", "battery_temp_c",
    ]


async def test_control_group_members(db):
    grp = await get_publishing_group(db, "control")
    assert grp is not None
    assert len(grp["members"]) == 8  # includes battery_command_target_soc


async def test_status_group_members(db):
    grp = await get_publishing_group(db, "status")
    assert grp is not None
    assert len(grp["members"]) == 39


# ── CRUD ───────────────────────────────────────────────────────


async def test_create_custom_group(db):
    grp = await create_publishing_group(
        db, "custom_test", "Custom Test", description="For testing",
        members=["battery_soc", "grid_power_kw"],
    )
    assert grp["slug"] == "custom_test"
    assert grp["name"] == "Custom Test"
    assert grp["is_default"] is False
    assert grp["enabled"] is True
    assert sorted(grp["members"]) == ["battery_soc", "grid_power_kw"]


async def test_update_group_name(db):
    grp = await update_publishing_group(db, "battery", name="My Battery")
    assert grp["name"] == "My Battery"
    assert grp["slug"] == "battery"


async def test_update_group_enabled(db):
    grp = await update_publishing_group(db, "battery", enabled=False)
    assert grp["enabled"] is False


async def test_update_nonexistent_returns_none(db):
    result = await update_publishing_group(db, "does_not_exist", name="X")
    assert result is None


async def test_delete_custom_group(db):
    await create_publishing_group(db, "temp", "Temporary")
    ok = await delete_publishing_group(db, "temp")
    assert ok is True
    grp = await get_publishing_group(db, "temp")
    assert grp is None


async def test_delete_default_group_raises(db):
    with pytest.raises(ValueError, match="Cannot delete default group"):
        await delete_publishing_group(db, "battery")


async def test_delete_nonexistent_returns_false(db):
    ok = await delete_publishing_group(db, "nope")
    assert ok is False


async def test_get_nonexistent_group_returns_none(db):
    grp = await get_publishing_group(db, "nope")
    assert grp is None


# ── Member management ──────────────────────────────────────────


async def test_set_group_members(db):
    members = await set_group_members(db, "battery", ["battery_soc"])
    assert members == ["battery_soc"]


async def test_add_group_member(db):
    await add_group_member(db, "battery", "grid_power_kw")
    members = await get_group_members(db, "battery")
    assert "grid_power_kw" in members


async def test_add_group_member_idempotent(db):
    before = await get_group_members(db, "battery")
    await add_group_member(db, "battery", "battery_soc")
    after = await get_group_members(db, "battery")
    assert len(after) == len(before)


async def test_remove_group_member(db):
    await remove_group_member(db, "battery", "battery_soc")
    members = await get_group_members(db, "battery")
    assert "battery_soc" not in members


# ── Disabled entity logic ──────────────────────────────────────


async def test_no_disabled_initially(db):
    disabled = await get_disabled_entity_slugs(db)
    assert len(disabled) == 0


async def test_disable_group_disables_entities(db):
    await update_publishing_group(db, "battery", enabled=False)
    disabled = await get_disabled_entity_slugs(db)
    assert "battery_soc" in disabled
    assert "battery_soh" in disabled
    assert "battery_power_kw" in disabled
    assert "battery_state" in disabled


async def test_entity_in_multiple_groups_stays_enabled(db):
    """Entity in both disabled and enabled groups is NOT disabled."""
    # Add battery_soc to the 'power' group (which stays enabled)
    await add_group_member(db, "power", "battery_soc")
    await update_publishing_group(db, "battery", enabled=False)
    disabled = await get_disabled_entity_slugs(db)
    # battery_soc is in power (enabled) + battery (disabled) → NOT disabled
    assert "battery_soc" not in disabled
    # battery_soh is only in battery (disabled) → IS disabled
    assert "battery_soh" in disabled


async def test_disable_all_groups_disables_all(db):
    groups = await get_publishing_groups(db)
    for g in groups:
        await update_publishing_group(db, g["slug"], enabled=False)
    disabled = await get_disabled_entity_slugs(db)
    assert len(disabled) == len(BRIDGE_ENTITIES)


async def test_reenable_group_reenables_entities(db):
    await update_publishing_group(db, "battery", enabled=False)
    disabled1 = await get_disabled_entity_slugs(db)
    assert "battery_soc" in disabled1

    await update_publishing_group(db, "battery", enabled=True)
    disabled2 = await get_disabled_entity_slugs(db)
    assert "battery_soc" not in disabled2


# ── Publisher integration ──────────────────────────────────────


async def test_publisher_sync_groups(db):
    """MqttPublisher.sync_groups filters entities based on group settings."""
    from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher

    pub = MqttPublisher(host="localhost")
    pub.set_ac_type(2)  # three-phase: include all per-phase entities

    # Initially all entities active
    await pub.sync_groups(db)
    assert len(pub.entities) == len(BRIDGE_ENTITIES)

    # Disable battery group
    await update_publishing_group(db, "battery", enabled=False)
    await pub.sync_groups(db)
    active_slugs = {e.slug for e in pub.entities}
    assert "battery_soc" not in active_slugs
    assert "grid_power_kw" in active_slugs

    # Re-enable
    await update_publishing_group(db, "battery", enabled=True)
    await pub.sync_groups(db)
    assert len(pub.entities) == len(BRIDGE_ENTITIES)


async def test_publisher_phase_and_group_combined(db):
    """Phase filtering and group filtering work together."""
    from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher

    pub = MqttPublisher(host="localhost")

    # Single-phase (ac_type=0): only phase=None and phase=1
    pub.set_ac_type(0)
    single_count = len(pub.entities)

    # Now also disable battery group
    await update_publishing_group(db, "battery", enabled=False)
    await pub.sync_groups(db)
    assert len(pub.entities) < single_count
    active_slugs = {e.slug for e in pub.entities}
    assert "battery_soc" not in active_slugs
    # Phase-2 entities should also be absent
    assert "grid_voltage_l2_v" not in active_slugs
