"""Scheduler v2 sensor registry — the namespace conditions evaluate against.

Maps the Bridge's canonical ``sample.points`` keys onto the stable sensor ids a
schedule's ``ConditionTree`` references (see ``scheduler_conditions``). The
engine builds a snapshot once per tick and evaluates every entry/exit tree
against that single consistent view.

**Source of truth = the cached latest poll sample, NOT fresh Modbus reads.**
The planning brief (§5) proposed getters that call ``controller.read_battery_status``
et al. That would add Modbus traffic on every tick, contend the per-gateway
``modbus_lock``, and risk an inconsistent tree (each read at a different instant).
Instead we read the already-cached ``GatewayInstance.latest_points()`` (documented
"no Modbus call"), which the poller refreshes on its own cadence. The engine
passes those points in; this module stays pure and unit-testable.

**Canonical point keys** (verified against ``entities.py`` stat_keys,
``aggregator.py``, ``command_handler.py`` — the plan's assumed names were wrong):

    soc                battery SOC %
    battery_power_w    battery power, signed (convention: negative = charging)
    total_solar        solar power W
    grid_power_w       grid power, signed
    home_load_ext      home load W
    mode_name          operating mode string (e.g. "TOU", "Self-Consumption",
                       "Emergency Backup")
    mode_raw           numeric operating-mode code (hardware-confirmed present in
                       the published sample alongside mode_name)
    connection_state   "Connected"/"Disconnected" — the off-grid signal the
                       aggregator derives grid presence from

All of the above were confirmed against a live aGate sample (see
``tests/hardware/test_scheduler_sensors_live.py``), not just the mock.

- ``price.export_c_per_kwh`` — kept as a stub returning ``None`` (fails closed in
  conditions) until a price source is wired.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

#: Solar power (W) above which ``pv.is_generating`` is True.
PV_GENERATING_THRESHOLD_W = 50.0

SensorKind = Literal["number", "bool", "enum"]
Points = dict[str, Any]


@dataclass(frozen=True)
class SensorDef:
    id: str
    label: str
    unit: str | None
    kind: SensorKind
    getter: Callable[[Points, datetime], Any]


def _num(points: Points, key: str) -> float | None:
    """Numeric point value as float, or None if absent/non-numeric."""
    v = points.get(key)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _grid_connected(points: Points, _now: datetime) -> bool | None:
    """Derive grid-connected from ``connection_state`` (the aggregator's off-grid
    signal), falling back to ``grid_mode`` ("Grid Following" = connected)."""
    cs = points.get("connection_state")
    if cs is not None:
        return "disconnect" not in str(cs).lower()
    gm = points.get("grid_mode")
    if gm is not None:
        return "following" in str(gm).lower()
    return None


def _mode_name(points: Points, _now: datetime) -> str | None:
    v = points.get("mode_name")
    return str(v) if v is not None else None


def _pv_generating(points: Points, _now: datetime) -> bool | None:
    solar = _num(points, "total_solar")
    if solar is None:
        return None
    return solar > PV_GENERATING_THRESHOLD_W


def _kwh(points: Points, key: str) -> float | None:
    """Lifetime cumulative energy in kWh from a Wh point, or None if absent.

    The aGate exposes monotonically-increasing lifetime counters over Modbus
    (701.TotWhAbs/Inj, 502.OutWh, 714.DCWhAbs/Inj) surfaced under these keys.
    Period totals (today/week/month/YTD) are counter deltas across a boundary —
    a separate stateful component; these are the raw lifetime figures.
    """
    wh = _num(points, key)
    return round(wh / 1000.0, 3) if wh is not None else None


SENSORS: list[SensorDef] = [
    SensorDef("battery.soc_pct", "Battery SOC (%)", "%", "number", lambda p, _n: _num(p, "soc")),
    SensorDef(
        "battery.power_w",
        "Battery Power (W, signed)",
        "W",
        "number",
        lambda p, _n: _num(p, "battery_power_w"),
    ),
    SensorDef(
        "solar.power_w", "Solar Power (W)", "W", "number", lambda p, _n: _num(p, "total_solar")
    ),
    SensorDef(
        "grid.power_w",
        "Grid Power (W, signed)",
        "W",
        "number",
        lambda p, _n: _num(p, "grid_power_w"),
    ),
    SensorDef("grid.connected", "Grid Connected", None, "bool", _grid_connected),
    SensorDef(
        "load.power_w", "Home Load (W)", "W", "number", lambda p, _n: _num(p, "home_load_ext")
    ),
    SensorDef("mode.name", "Operating Mode", None, "enum", _mode_name),
    SensorDef(
        "mode.raw", "Operating Mode (code)", None, "number", lambda p, _n: _num(p, "mode_raw")
    ),
    SensorDef("pv.is_generating", "PV Generating", None, "bool", _pv_generating),
    # ── Lifetime cumulative energy (kWh, from Modbus Wh counters) ──
    SensorDef(
        "energy.grid_import.total_kwh",
        "Grid Import — lifetime (kWh)",
        "kWh",
        "number",
        lambda p, _n: _kwh(p, "grid_import_wh"),
    ),
    SensorDef(
        "energy.grid_export.total_kwh",
        "Grid Export — lifetime (kWh)",
        "kWh",
        "number",
        lambda p, _n: _kwh(p, "grid_export_wh"),
    ),
    SensorDef(
        "energy.solar.total_kwh",
        "Solar — lifetime (kWh)",
        "kWh",
        "number",
        lambda p, _n: _kwh(p, "pv_energy_total_wh"),
    ),
    SensorDef(
        "energy.battery_charge.total_kwh",
        "Battery Charged — lifetime (kWh)",
        "kWh",
        "number",
        lambda p, _n: _kwh(p, "dc_energy_charged_wh"),
    ),
    SensorDef(
        "energy.battery_discharge.total_kwh",
        "Battery Discharged — lifetime (kWh)",
        "kWh",
        "number",
        lambda p, _n: _kwh(p, "dc_energy_discharged_wh"),
    ),
    SensorDef("time.hour", "Hour of day (0..23, local)", None, "number", lambda _p, n: n.hour),
    SensorDef("time.dow", "Day of week (0=Mon..6=Sun)", None, "number", lambda _p, n: n.weekday()),
    SensorDef("time.month", "Month (1..12)", None, "number", lambda _p, n: n.month),
    SensorDef(
        "price.export_c_per_kwh", "Export Price (c/kWh)", "c/kWh", "number", lambda _p, _n: None
    ),  # stub until a price source is wired
]

# ── Period energy totals (today/week/month/YTD, kWh) ──────────
# Values come from the EnergyTotals component as `energy_<source>_<period>_kwh`
# points merged into the gateway's points (see main.py); mapped 1:1 to sensors.
_ENERGY_SOURCES = {
    "grid_import": "Grid Import",
    "grid_export": "Grid Export",
    "solar": "Solar",
    "battery_charge": "Battery Charged",
    "battery_discharge": "Battery Discharged",
}
_ENERGY_PERIODS = {
    "today": "today",
    "this_week": "this week",
    "this_month": "this month",
    "ytd": "YTD",
}


def _energy_getter(point_key: str):
    return lambda p, _n: _num(p, point_key)


for _src, _slabel in _ENERGY_SOURCES.items():
    for _per, _plabel in _ENERGY_PERIODS.items():
        SENSORS.append(
            SensorDef(
                f"energy.{_src}.{_per}_kwh",
                f"{_slabel} — {_plabel} (kWh)",
                "kWh",
                "number",
                _energy_getter(f"energy_{_src}_{_per}_kwh"),
            )
        )


def snapshot(points: Points, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate every sensor against ``points`` + clock into a flat id→value dict.

    Every static sensor id is always present; an unavailable value is ``None``
    (which the condition evaluator treats as fail-closed). External sensor values
    already keyed as sensor ids in ``points`` — HA entities ``ha:<inst>:<entity>``
    merged in by the app — pass through unchanged so conditions can reference them.
    """
    now = now or datetime.now()
    snap = {s.id: s.getter(points, now) for s in SENSORS}
    for k, v in points.items():
        if isinstance(k, str) and k.startswith("ha:"):
            snap[k] = v
    return snap


def sensor_catalog(points: Points | None = None, now: datetime | None = None) -> list[dict]:
    """Registry metadata for the UI dropdowns (``/api/sensors``).

    When ``points`` is given, each entry also carries its current ``value``.
    """
    now = now or datetime.now()
    out = []
    for s in SENSORS:
        row = {"id": s.id, "label": s.label, "unit": s.unit, "kind": s.kind}
        if points is not None:
            row["value"] = s.getter(points, now)
        out.append(row)
    return out
