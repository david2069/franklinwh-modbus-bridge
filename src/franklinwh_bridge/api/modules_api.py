"""Feature-module REST — list modules + admin enable/disable toggles, and a
``require_module`` dependency that gates a feature's routes when it's disabled.

Phase 0: no auth yet, so the caller is the single implicit admin (all
capabilities); only the global ``enabled`` flag applies. Roles slot in at Phase 2
(docs/multi-user-and-pwa-design.md §M/§8).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from franklinwh_bridge.gateway.modules import (
    get_modules,
    is_module_enabled,
    set_module_enabled,
)

router = APIRouter(prefix="/api", tags=["modules"])


class ModuleToggle(BaseModel):
    enabled: bool


@router.get("/modules")
async def list_modules(request: Request):
    """All feature modules with resolved enabled state (+ can_access — always true
    pre-auth). Drives the UI nav/tab gating and the admin Modules panel."""
    return {"modules": await get_modules(request.app.state.db)}


@router.patch("/modules/{module_id}")
async def toggle_module(module_id: str, body: ModuleToggle, request: Request):
    """Enable/disable a module (admin). Core modules can't be disabled."""
    try:
        return await set_module_enabled(request.app.state.db, module_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(404, f"module '{module_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def require_module(module_id: str):
    """Dependency: 403 a feature's routes when its module is disabled. Apply at
    ``include_router(..., dependencies=[Depends(require_module("automations"))])``."""

    async def _dep(request: Request) -> None:
        if not await is_module_enabled(request.app.state.db, module_id):
            raise HTTPException(403, f"module '{module_id}' is disabled")

    return Depends(_dep)
