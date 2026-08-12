"""Feature-module registry (Phase 0): enabled flags, core-lock, capability gating."""

import pytest

from franklinwh_bridge.gateway.modules import (
    get_modules,
    is_module_enabled,
    set_module_enabled,
)
from franklinwh_bridge.store.db import init_db


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "mod.db")
    yield conn
    await conn.close()


async def test_defaults_all_enabled_core_flagged(db):
    mods = {m["id"]: m for m in await get_modules(db)}
    assert mods["dashboard"]["core"] is True and mods["settings"]["core"] is True
    assert mods["automations"]["core"] is False
    assert all(m["enabled"] for m in mods.values())  # default: everything on
    # pre-auth (no capabilities filter) → implicit admin can access all
    assert all(m["can_access"] for m in mods.values())
    assert mods["automations"]["tab"] == "schedule"  # module id != tab id


async def test_disable_and_reenable_non_core(db):
    m = await set_module_enabled(db, "automations", False)
    assert m["enabled"] is False
    assert await is_module_enabled(db, "automations") is False
    mods = {x["id"]: x for x in await get_modules(db)}
    assert mods["automations"]["enabled"] is False
    assert mods["ha_entities"]["enabled"] is True  # others unaffected

    await set_module_enabled(db, "automations", True)
    assert await is_module_enabled(db, "automations") is True


async def test_core_cannot_be_disabled(db):
    with pytest.raises(ValueError, match="core"):
        await set_module_enabled(db, "dashboard", False)
    assert await is_module_enabled(db, "dashboard") is True


async def test_unknown_module_raises(db):
    with pytest.raises(KeyError):
        await set_module_enabled(db, "nope", False)
    # is_module_enabled is defensive: unknown id doesn't block
    assert await is_module_enabled(db, "nope") is True


async def test_capability_gating(db):
    # a view-only principal (Phase 2 'user'/'viewer') sees only 'view' modules
    mods = {m["id"]: m for m in await get_modules(db, capabilities={"view"})}
    assert mods["dashboard"]["can_access"] is True  # capability == view
    assert mods["automations"]["can_access"] is False
    assert mods["ha_entities"]["can_access"] is False
