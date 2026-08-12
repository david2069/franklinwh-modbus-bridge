"""Feature-module registry (Phase 0 of the modularisation layer).

Every feature is a **module** with a global ``enabled`` flag (an admin kill
switch, persisted in ``app_config``) and a ``capability`` (roles gate who sees
enabled ones — roles land in Phase 2; until then a single implicit admin holds
every capability, so only ``enabled`` applies). One substrate handles both "turn
a feature off for everyone" and (later) "hide it from a role".

See docs/multi-user-and-pwa-design.md §M.
"""

from __future__ import annotations

import json
from typing import Any

from franklinwh_bridge.store.db import get_app_config, set_app_config

#: app_config key holding the JSON override map ``{module_id: bool}``.
_MODULES_KEY = "modules_enabled"

# id · label · tab (activeTab value) · capability · core (undisableable) · default
MODULES: list[dict] = [
    {"id": "dashboard", "label": "Dashboard", "tab": "dashboard",
     "capability": "view", "core": True, "default_enabled": True},
    {"id": "explorer", "label": "SunSpec Explorer", "tab": "explorer",
     "capability": "explorer", "core": False, "default_enabled": True},
    {"id": "logs", "label": "Logs", "tab": "logs",
     "capability": "logs", "core": False, "default_enabled": True},
    {"id": "sequencer", "label": "Sequencer", "tab": "sequencer",
     "capability": "sequencer", "core": False, "default_enabled": True},
    {"id": "automations", "label": "Schedule", "tab": "schedule",
     "capability": "automations", "core": False, "default_enabled": True},
    {"id": "ha_entities", "label": "HA Entities", "tab": "ha_entities",
     "capability": "ha_entities", "core": False, "default_enabled": True},
    {"id": "settings", "label": "Settings", "tab": "settings",
     "capability": "settings", "core": True, "default_enabled": True},
]
_BY_ID = {m["id"]: m for m in MODULES}
_PUBLIC = ("id", "label", "tab", "capability", "core")

#: Roles → capabilities. Phase 2 wires this to users; for now a single implicit
#: admin holds every capability. Kept here as the single source of truth.
ROLE_CAPABILITIES: dict[str, set[str]] = {
    "admin": {m["capability"] for m in MODULES} | {"control", "users"},
    "user": {"view"},
    "viewer": {"view"},
}


async def _overrides(db: Any) -> dict:
    raw = await get_app_config(db, _MODULES_KEY, None)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _resolved_enabled(module: dict, overrides: dict) -> bool:
    if module["core"]:
        return True
    return bool(overrides.get(module["id"], module["default_enabled"]))


async def get_modules(db: Any, *, capabilities: set[str] | None = None) -> list[dict]:
    """All modules with resolved ``enabled`` and, when ``capabilities`` is given,
    whether the caller may access. ``None`` = the implicit admin (all caps)."""
    ov = await _overrides(db)
    out = []
    for m in MODULES:
        out.append(
            {
                **{k: m[k] for k in _PUBLIC},
                "enabled": _resolved_enabled(m, ov),
                "can_access": capabilities is None or m["capability"] in capabilities,
            }
        )
    return out


async def is_module_enabled(db: Any, module_id: str) -> bool:
    m = _BY_ID.get(module_id)
    if m is None:
        return True  # unknown id → don't block (defensive)
    return _resolved_enabled(m, await _overrides(db))


async def set_module_enabled(db: Any, module_id: str, enabled: bool) -> dict:
    m = _BY_ID.get(module_id)
    if m is None:
        raise KeyError(module_id)
    if m["core"] and not enabled:
        raise ValueError(f"module '{module_id}' is core and cannot be disabled")
    ov = await _overrides(db)
    ov[module_id] = bool(enabled)
    await set_app_config(db, _MODULES_KEY, json.dumps(ov))
    return {**{k: m[k] for k in _PUBLIC}, "enabled": bool(enabled)}
