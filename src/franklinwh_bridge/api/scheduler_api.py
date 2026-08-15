"""Scheduler v2 REST — sensor namespace + ad-hoc condition evaluation.

These two endpoints power the FWHAI-parity Automation Builder UI: ``/api/sensors``
fills the condition-row sensor dropdowns with live values, and
``/api/scheduler/evaluate`` backs the **Test Verification** button (evaluate a
condition tree against the gateway's current sensor snapshot before deploying).

The v2 job CRUD lives on the existing ``/api/schedules`` surface (extended for
trigger/condition fields) rather than a duplicate ``/api/scheduler/jobs``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.gateway.constants import ConstantsStore
from franklinwh_bridge.gateway.scheduler_conditions import evaluate_dict
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog, snapshot

router = APIRouter(prefix="/api", tags=["scheduler"])


def _gateway_points(request: Request, gateway: str) -> dict:
    """Latest cached points for a gateway (no Modbus call) + period energy totals
    + HA entity values, or {} if unknown — same view the engine evaluates
    conditions against."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return {}
    inst = registry.get(gateway)
    pts = inst.latest_points() if inst else {}
    energy = getattr(request.app.state, "energy_totals", None)
    if energy is not None:
        pts = {**pts, **energy.current_totals(gateway)}
    ha = getattr(request.app.state, "ha_registry", None)
    if ha is not None:
        pts = {**pts, **ha.entity_values()}
    constants = getattr(request.app.state, "constants", None)
    if constants is not None:
        pts = {**pts, **constants.as_points()}
    billing = getattr(request.app.state, "billing", None)
    if billing is not None:
        pts = {**pts, **billing.as_points()}
    demand = getattr(request.app.state, "demand_tracker", None)
    if demand is not None:
        pts = {**pts, **demand.as_points()}
    return pts


@router.get("/sensors")
async def list_sensors(request: Request, gateway: str = "default"):
    """Sensor namespace + current values for the condition-builder dropdowns.

    Includes the static Bridge sensors plus any HA entities (``ha:<inst>:<entity>``)
    so the Automation Builder can condition on Home Assistant state."""
    points = _gateway_points(request, gateway)
    sensors = sensor_catalog(points)
    ha = getattr(request.app.state, "ha_registry", None)
    if ha is not None:
        sensors = [*sensors, *ha.catalog()]
    return {"gateway": gateway, "sensors": sensors}


class ConstantsBody(BaseModel):
    min_discharge_soc: float | None = Field(default=None, ge=0, le=100)
    max_charge_soc: float | None = Field(default=None, ge=0, le=100)
    demand_charge_min_soc: float | None = Field(default=None, ge=0, le=100)


@router.get("/automation/constants")
async def get_constants(request: Request):
    """User-defined automation constants (min/max/demand SoC) + their [min,max]
    bounds for the settings form. Surfaced as ``const.*`` sensors too."""
    store = getattr(request.app.state, "constants", None)
    if store is None:
        return {"values": {}, "spec": {}}
    return {"values": store.values(), "spec": ConstantsStore.spec()}


@router.put("/automation/constants")
async def put_constants(body: ConstantsBody, request: Request):
    """Update automation constants (only sent fields; each clamped to [0,100])."""
    store = getattr(request.app.state, "constants", None)
    if store is None:
        raise HTTPException(503, "Constants store not available")
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    values = await store.update(updates)
    return {"values": values, "spec": ConstantsStore.spec()}


class ConditionTreeBody(BaseModel):
    match: str = Field(default="ALL", pattern=r"^(ALL|ANY|all|any)$")
    # conditions are Condition or nested ConditionTree dicts — the evaluator is
    # defensive about shape, so accept raw objects rather than over-constrain.
    conditions: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/scheduler/evaluate")
async def evaluate_conditions(body: ConditionTreeBody, request: Request, gateway: str = "default"):
    """Evaluate a condition tree against the gateway's live snapshot.

    Returns ``{result: bool, per_condition: [{sensor, op, value, live_value,
    result}]}`` — the Test Verification response.
    """
    snap = snapshot(_gateway_points(request, gateway))
    return evaluate_dict(body.model_dump(), snap)
