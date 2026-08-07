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

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.gateway.scheduler_conditions import evaluate_dict
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog, snapshot

router = APIRouter(prefix="/api", tags=["scheduler"])


def _gateway_points(request: Request, gateway: str) -> dict:
    """Latest cached points for a gateway (no Modbus call), or {} if unknown."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return {}
    inst = registry.get(gateway)
    return inst.latest_points() if inst else {}


@router.get("/sensors")
async def list_sensors(request: Request, gateway: str = "default"):
    """Sensor namespace + current values for the condition-builder dropdowns."""
    return {"gateway": gateway, "sensors": sensor_catalog(_gateway_points(request, gateway))}


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
