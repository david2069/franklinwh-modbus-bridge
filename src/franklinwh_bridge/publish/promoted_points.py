"""Promoted SunSpec points → HA entities.

Publishing Groups can include not just curated entities but raw SunSpec
catalog points "promoted" into publishing. This module turns a catalog point
(model.point + its metadata) into a standard read-only ``EntityDef`` so it
flows through the existing discovery/publish pipeline unchanged.

P1 is read-only (sensors). Writable-point promotion (number/select) is gated
to a later phase — see docs/publishing-groups-design.md.
"""

from __future__ import annotations

from franklinwh_bridge.publish.entities import EntityDef

# HA device_class inferred from a point's unit.
_UNIT_DEVICE_CLASS: dict[str, str] = {
    "W": "power",
    "kW": "power",
    "Wh": "energy",
    "kWh": "energy",
    "VA": "apparent_power",
    "var": "reactive_power",
    "V": "voltage",
    "A": "current",
    "Hz": "frequency",
    "C": "temperature",
    "°C": "temperature",
}

# SunSpec types that are not plain numeric measurements.
_NON_NUMERIC_TYPES = {
    "string", "enum16", "enum32", "bitfield16", "bitfield32", "pad", "sunssf",
}


def promoted_slug(model_id: int, point_name: str) -> str:
    """Collision-free slug for a promoted point (curated slugs are never m<n>_…)."""
    return f"m{model_id}_{point_name}".lower()


def point_entity_def(
    model_id: int,
    point_name: str,
    *,
    dtype: str = "",
    unit: str = "",
    label: str = "",
    disp_name: str | None = None,
    disp_unit: str | None = None,
) -> EntityDef:
    """Build a read-only ``EntityDef`` for a promoted catalog point.

    The state value comes from ``points["<model>.<point>"]`` (already scaled by
    the SunSpec library on read), so no extra scaling is applied. unit and
    device_class are inferred from the catalog metadata; both can be overridden
    per group membership (``disp_name`` / ``disp_unit``).
    """
    u = (disp_unit if disp_unit is not None else unit) or ""
    numeric = dtype not in _NON_NUMERIC_TYPES
    device_class = _UNIT_DEVICE_CLASS.get(u, "") if numeric else ""
    state_class = ""
    if numeric:
        state_class = "total_increasing" if device_class == "energy" else "measurement"

    return EntityDef(
        slug=promoted_slug(model_id, point_name),
        name=disp_name or f"{model_id} {label or point_name}",
        ha_type="sensor",
        state_group="diagnostic",
        stat_key=f"{model_id}.{point_name}",
        unit=u if numeric else "",
        device_class=device_class,
        state_class=state_class,
        entity_category="diagnostic",
        source=f"{model_id}.{point_name}",
    )
