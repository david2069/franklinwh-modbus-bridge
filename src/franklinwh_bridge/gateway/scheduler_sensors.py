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

There is intentionally **no price.* sensor** yet: a price source isn't wired, and
a stub that always returns ``None`` just clutters the condition picker. Add one
(mapped to a real point) when the pricing integration lands.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from franklinwh_bridge.config import clock as _clock

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


def _text(points: Points, *keys: str) -> str | None:
    """First non-empty string among ``keys``, else None.

    SunSpec Model 1 (Common) reaches the snapshot twice: as friendly keys
    (``serial``, ``model``) and as raw points (``1.SN``, ``1.Md``). Which are
    populated depends on the device — a MAC-1 collar publishes a smaller set —
    so read the friendly name first and fall back to the register.
    """
    for k in keys:
        v = points.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _tz_matches_clock(points: Points) -> bool | None:
    """Does the plan's timezone agree with the clock windows are evaluated on?

    Compares the **current UTC offsets**, not the zone names: that is the
    functional question (do the plan's windows land at the intended wall-clock
    time?), and comparing at a single instant handles DST on both sides. Name
    comparison would also call ``UTC`` and ``Etc/UTC`` a mismatch.

    None when the service hasn't stated a timezone, or states one this system
    can't resolve — unknown must not read as a failure.
    """
    name = (points.get("service_timezone") or "").strip()
    if not name:
        return None
    try:
        plan_offset = datetime.now(UTC).astimezone(ZoneInfo(name)).utcoffset()
    except Exception:
        return None
    if plan_offset is None:
        return None
    return plan_offset == timedelta(seconds=_time.localtime().tm_gmtoff)


def _rate_now(points: Points, now: datetime) -> dict:
    """Resolved season/wave/prices for this instant (pure, cheap)."""
    from franklinwh_bridge.gateway.rate_model import resolve

    return resolve(points.get("tariff_seasons"), now)


def _import_billable(points: Points, now: datetime) -> bool | None:
    """Is grid import billable right now?

    Default True: with no free window declared, import costs money. Only an
    explicitly non-billable window makes it False, so a misconfiguration reads
    as "you are paying" rather than inviting an automation to charge for free
    when it isn't.
    """
    # The rate model is authoritative once configured: a wave priced at zero
    # IS a free window, so the two can't disagree.
    resolved = _rate_now(points, now)
    if resolved.get("billable") is not None:
        return resolved["billable"]

    windows = points.get("tariff_import_windows")
    if not windows:
        return None
    for w in windows:
        if isinstance(w, dict) and _in_window(w, now) and not w.get("billable", True):
            return False
    return True


def _mode_name(points: Points, _now: datetime) -> str | None:
    v = points.get("mode_name")
    return str(v) if v is not None else None


def _reserve_current(points: Points, _now: datetime) -> float | None:
    """The reserve-SOC floor that applies to the CURRENT operating mode.

    TOU mode → tou_reserve_pct; anything else (Self-Consumption / Backup) →
    self_reserve_pct, each with a fallback to the other. Today the aGate keeps
    both at the same value over Modbus, so this is effectively either; it becomes
    meaningful if/when they diverge — so an automation can just track "the reserve
    in force right now" without caring about the mode.
    """
    self_r = _num(points, "self_reserve_pct")
    tou_r = _num(points, "tou_reserve_pct")
    mode = str(points.get("mode_name") or "").lower()
    if "tou" in mode or "time of use" in mode or "time-of-use" in mode:
        return tou_r if tou_r is not None else self_r
    return self_r if self_r is not None else tou_r


def _pv_generating(points: Points, _now: datetime) -> bool | None:
    solar = _num(points, "total_solar")
    if solar is None:
        return None
    return solar > PV_GENERATING_THRESHOLD_W


# ── Derived battery / inverter fields ─────────────────────────
# Computed from live points (+ user constants merged in as const_*), so
# automations can condition on capacity/headroom/power-headroom/ETA without the
# user doing the arithmetic. All fail-closed (None) when an input is missing.
def _capacity_kwh(p: Points, _n: datetime) -> float | None:
    wh = _num(p, "wh_rating")  # 713.WHRtg — battery plate capacity
    return round(wh / 1000.0, 2) if wh else None


def _stored_kwh(p: Points, _n: datetime) -> float | None:
    cap = _capacity_kwh(p, _n)
    soc = _num(p, "soc")
    if cap is None or soc is None:
        return None
    return round(cap * soc / 100.0, 2)


def _remaining_kwh(p: Points, _n: datetime) -> float | None:
    """Headroom to full = plate capacity − current stored energy."""
    cap = _capacity_kwh(p, _n)
    stored = _stored_kwh(p, _n)
    if cap is None or stored is None:
        return None
    return round(cap - stored, 2)


def _inverter_rating_w(p: Points, _n: datetime) -> float | None:
    c = _num(p, "max_charge_rate_w")
    d = _num(p, "max_discharge_rate_w")
    vals = [x for x in (c, d) if x is not None]
    return max(vals) if vals else None


def _utilised_w(p: Points, _n: datetime) -> float | None:
    bw = _num(p, "battery_power_w")  # signed; magnitude = power in use
    return abs(bw) if bw is not None else None


def _unutilised_w(p: Points, _n: datetime) -> float | None:
    r = _inverter_rating_w(p, _n)
    u = _utilised_w(p, _n)
    if r is None or u is None:
        return None
    return round(max(0.0, r - u), 1)


def _eta_min(cap: float | None, stored: float | None, target_soc: float | None,
             rate_w: float | None, *, charging: bool) -> float | None:
    """Minutes to reach ``target_soc`` at ``rate_w`` (best case, constant rate)."""
    if cap is None or stored is None or target_soc is None or not rate_w:
        return None
    target_kwh = cap * target_soc / 100.0
    delta = (target_kwh - stored) if charging else (stored - target_kwh)
    if delta <= 0:
        return 0.0
    return round(delta / (rate_w / 1000.0) * 60.0, 1)


def _time_to_charge_min(p: Points, n: datetime) -> float | None:
    """ETA to the user's Max-Charge SoC at the max charge rate."""
    return _eta_min(
        _capacity_kwh(p, n), _stored_kwh(p, n),
        _num(p, "const_max_charge_soc"), _num(p, "max_charge_rate_w"), charging=True,
    )


def _time_to_discharge_min(p: Points, n: datetime) -> float | None:
    """ETA to the user's Min-Discharge SoC at the max discharge rate."""
    return _eta_min(
        _capacity_kwh(p, n), _stored_kwh(p, n),
        _num(p, "const_min_discharge_soc"), _num(p, "max_discharge_rate_w"), charging=False,
    )


def _eta_now_min(cap: float | None, stored: float | None, target_soc: float | None,
                 signed_w: float | None, *, charging: bool) -> float | None:
    """ETA at the CURRENT inverter rate. ``signed_w`` is battery_power_w
    (negative = charging, positive = discharging). Returns None when the battery
    isn't moving toward the target (idle or the wrong direction — the ETA would
    be infinite), 0.0 when already at/past it."""
    if cap is None or stored is None or target_soc is None or signed_w is None:
        return None
    target_kwh = cap * target_soc / 100.0
    if charging:
        rate_kw = -signed_w / 1000.0     # charging draws power negative
        delta = target_kwh - stored
    else:
        rate_kw = signed_w / 1000.0      # discharging pushes power positive
        delta = stored - target_kwh
    if rate_kw <= 0:                      # not moving toward target
        return None
    if delta <= 0:                        # already there
        return 0.0
    return round(delta / rate_kw * 60.0, 1)


def _time_to_charge_now_min(p: Points, n: datetime) -> float | None:
    """ETA to Max-Charge SoC at the CURRENT charge rate (None if not charging)."""
    return _eta_now_min(
        _capacity_kwh(p, n), _stored_kwh(p, n),
        _num(p, "const_max_charge_soc"), _num(p, "battery_power_w"), charging=True,
    )


def _time_to_discharge_now_min(p: Points, n: datetime) -> float | None:
    """ETA to Min-Discharge SoC at the CURRENT discharge rate (None if not
    discharging)."""
    return _eta_now_min(
        _capacity_kwh(p, n), _stored_kwh(p, n),
        _num(p, "const_min_discharge_soc"), _num(p, "battery_power_w"), charging=False,
    )


# ── Tariff windows (utility billing) ──────────────────────────
def _hhmm(raw: object) -> tuple[int, int] | None:
    """'HH:MM' → (hour, minute), or None if malformed/out of range."""
    try:
        h_str, m_str = str(raw).strip().split(":")
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def _in_window(win: dict, now: datetime) -> bool:
    """Is ``now`` inside a tariff window {months, days, start, end}? Empty
    months/days = all. Supports a same-day range and an overnight wrap
    (start > end, e.g. 22:00–06:00)."""
    months = {int(m) for m in (win.get("months") or []) if 1 <= int(m) <= 12}
    if months and now.month not in months:
        return False
    days = {int(d) for d in (win.get("days") or []) if 0 <= int(d) <= 6}
    if days and now.weekday() not in days:
        return False
    hm_s = _hhmm(win.get("start"))
    hm_e = _hhmm(win.get("end"))
    if hm_s is None or hm_e is None:
        return True  # no time bound → whole day (subject to month/day)
    cur = now.hour * 60 + now.minute
    start = hm_s[0] * 60 + hm_s[1]
    end = hm_e[0] * 60 + hm_e[1]
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # overnight wrap


def _any_window_active(points: Points, key: str, now: datetime) -> bool:
    wins = points.get(key)
    if not isinstance(wins, list):
        return False
    return any(isinstance(w, dict) and _in_window(w, now) for w in wins)


def _demand_charge(p: Points, _n: datetime) -> float | None:
    """Demand charge to date this period. Basis (user-configurable):
    ``per_kw_day`` → peak_kW × rate × days-in-period; ``flat_per_kw`` →
    peak_kW × rate."""
    peak = _num(p, "demand_peak_kw")
    rate = _num(p, "tariff_demand_rate")
    if peak is None or rate is None:
        return None
    basis = p.get("tariff_demand_charge_basis") or "per_kw_day"
    if basis == "flat_per_kw":
        return round(peak * rate, 2)
    days = _num(p, "demand_days_in_period") or 0.0
    return round(peak * rate * days, 2)


def _bonus_credit(p: Points, _n: datetime) -> float | None:
    """bonus-window export (kWh) × export_bonus_rate ($/kWh)."""
    kwh = _num(p, "bonus_export_kwh")
    rate = _num(p, "tariff_export_bonus_rate")
    if kwh is None or rate is None:
        return None
    return round(kwh * rate, 2)


def _charge_net_kwh(p: Points, _n: datetime) -> float | None:
    """Export-charge kWh above the monthly free allowance (what actually gets
    charged). = max(0, charge_export_kwh − free_kwh)."""
    kwh = _num(p, "charge_export_kwh")
    free = _num(p, "charge_free_kwh")
    if kwh is None:
        return None
    return round(max(0.0, kwh - (free or 0.0)), 3)


def _charge_free_remaining(p: Points, _n: datetime) -> float | None:
    """Free-export headroom left this period = max(0, free_kwh − charge kWh).
    The key gate for 'self-consume midday once the free allowance is used up'."""
    kwh = _num(p, "charge_export_kwh")
    free = _num(p, "charge_free_kwh")
    if kwh is None or free is None:
        return None
    return round(max(0.0, free - kwh), 3)


def _charge_cost(p: Points, _n: datetime) -> float | None:
    """Export-charge cost so far = net kWh (above free) × export_charge_rate."""
    net = _charge_net_kwh(p, _n)
    rate = _num(p, "tariff_export_charge_rate")
    if net is None or rate is None:
        return None
    return round(net * rate, 2)


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
    # Reserve-SOC setpoints (the "floor" the aGate holds). Exposed so an
    # automation can compare live SOC against the reserve — e.g. force-charge
    # when battery.soc_pct < battery.reserve_pct (Lookup RHS). `reserve_pct`
    # follows the active mode; the mode-specific ones are also exposed. Points:
    # self_reserve_pct = ext.15508, tou_reserve_pct = ext.15509.
    SensorDef(
        "battery.reserve_pct",
        "Reserve SOC — current mode (%)",
        "%",
        "number",
        _reserve_current,
    ),
    SensorDef(
        "battery.reserve_self_pct",
        "Reserve SOC — Self-Consumption (%)",
        "%",
        "number",
        lambda p, _n: _num(p, "self_reserve_pct"),
    ),
    SensorDef(
        "battery.reserve_tou_pct",
        "Reserve SOC — TOU (%)",
        "%",
        "number",
        lambda p, _n: _num(p, "tou_reserve_pct"),
    ),
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
    # Device state strings. Polled all along but never exposed, so a rule could
    # act on power numbers yet not on "is the inverter actually running", and a
    # notification couldn't quote it.
    SensorDef(
        "inverter.status", "Inverter State", None, "enum",
        lambda p, _n: _text(p, "inverter_state"),
    ),
    SensorDef(
        "battery.status", "Battery State (Charging / Discharging / Standby)", None, "enum",
        lambda p, _n: _text(p, "battery_state"),
    ),
    SensorDef(
        "grid.status", "Grid Connection State", None, "enum",
        lambda p, _n: _text(p, "connection_state"),
    ),

    # ── Device identity ──────────────────────────────────────
    # Serial/model are already polled from the nameplate but weren't reachable
    # from an automation. With several gateways — and with MAC-1 collars beside
    # aGates — a rule needs to be able to say WHICH box it means, and a serial
    # is the only identifier that survives renaming a gateway.
    SensorDef(
        "gateway.serial", "Gateway Serial Number", None, "enum",
        lambda p, _n: _text(p, "serial", "1.SN"),
    ),
    SensorDef(
        "gateway.model", "Gateway Model (from nameplate)", None, "enum",
        lambda p, _n: _text(p, "model", "1.Md"),
    ),
    SensorDef(
        "gateway.device_type", "Gateway Type (agate / mac1)", None, "enum",
        lambda p, _n: str(p.get("device_type") or "agate"),
    ),
    SensorDef(
        "gateway.manufacturer", "Gateway Manufacturer (1.Mn)", None, "enum",
        lambda p, _n: _text(p, "manufacturer", "1.Mn"),
    ),
    SensorDef(
        "gateway.version", "Gateway Firmware Version (1.Vr)", None, "enum",
        lambda p, _n: _text(p, "version", "1.Vr"),
    ),
    SensorDef(
        "gateway.options", "Gateway Options (1.Opt)", None, "enum",
        lambda p, _n: _text(p, "options", "1.Opt"),
    ),
    SensorDef(
        "gateway.battery_capable", "Gateway can accept battery commands", None, "bool",
        lambda p, _n: str(p.get("device_type") or "agate") != "mac1",
    ),
    SensorDef(
        "mode.raw", "Operating Mode (code)", None, "number", lambda p, _n: _num(p, "mode_raw")
    ),
    SensorDef("pv.is_generating", "PV Generating", None, "bool", _pv_generating),
    # ── Derived battery / inverter fields ──
    SensorDef("battery.capacity_kwh", "Battery Capacity (kWh)", "kWh", "number", _capacity_kwh),
    SensorDef("battery.stored_kwh", "Battery Stored Energy (kWh)", "kWh", "number", _stored_kwh),
    SensorDef(
        "battery.remaining_kwh", "Battery Headroom to Full (kWh)", "kWh", "number", _remaining_kwh
    ),
    SensorDef(
        "inverter.power_rating_w", "Inverter Power Rating (W)", "W", "number", _inverter_rating_w
    ),
    SensorDef("inverter.utilised_w", "Inverter Power In Use (W)", "W", "number", _utilised_w),
    SensorDef(
        "inverter.unutilised_w", "Inverter Power Headroom (W)", "W", "number", _unutilised_w
    ),
    SensorDef(
        "battery.time_to_charge_min",
        "ETA to Max-Charge SoC (min)", "min", "number", _time_to_charge_min,
    ),
    SensorDef(
        "battery.time_to_discharge_min",
        "ETA to Min-Discharge SoC (min)", "min", "number", _time_to_discharge_min,
    ),
    SensorDef(
        "battery.time_to_charge_now_min",
        "ETA to Max-Charge SoC — at current rate (min)", "min", "number",
        _time_to_charge_now_min,
    ),
    SensorDef(
        "battery.time_to_discharge_now_min",
        "ETA to Min-Discharge SoC — at current rate (min)", "min", "number",
        _time_to_discharge_now_min,
    ),
    # ── User-defined constants (min/max/demand SoC) ──
    SensorDef(
        "const.min_discharge_soc", "Min Discharge SoC (%)", "%", "number",
        lambda p, _n: _num(p, "const_min_discharge_soc"),
    ),
    SensorDef(
        "const.max_charge_soc", "Max Charge SoC (%)", "%", "number",
        lambda p, _n: _num(p, "const_max_charge_soc"),
    ),
    SensorDef(
        "const.demand_charge_min_soc", "Demand-Charge Min SoC (%)", "%", "number",
        lambda p, _n: _num(p, "const_demand_charge_min_soc"),
    ),
    # ── Tariff windows (from utility-service billing config) ──
    SensorDef(
        "tariff.demand_window_active", "In Peak-Demand Window", None, "bool",
        lambda p, n: _any_window_active(p, "tariff_demand_windows", n),
    ),
    SensorDef(
        "tariff.bonus_window_active", "In Battery-Export-Bonus Window", None, "bool",
        lambda p, n: _any_window_active(p, "tariff_bonus_windows", n),
    ),
    # ── Demand-charge + battery-bonus calculator (DemandTracker) ──
    SensorDef(
        "demand.peak_kw", "Peak Demand this period (kW)", "kW", "number",
        lambda p, _n: _num(p, "demand_peak_kw"),
    ),
    SensorDef(
        "demand.interval_kw", "Current 30-min Demand (kW, running)", "kW", "number",
        lambda p, _n: _num(p, "demand_interval_kw"),
    ),
    SensorDef(
        "demand.period_charge", "Demand Charge this period ($)", "$", "number",
        _demand_charge,
    ),
    SensorDef(
        "bonus.export_kwh", "Bonus-window Export this period (kWh)", "kWh", "number",
        lambda p, _n: _num(p, "bonus_export_kwh"),
    ),
    SensorDef(
        "bonus.period_credit", "Battery Export Bonus this period ($)", "$", "number",
        _bonus_credit,
    ),
    # ── Export charge (two-way / solar-sponge) ──
    SensorDef(
        "tariff.export_charge_window_active", "In Export-Charge Window", None, "bool",
        lambda p, n: _any_window_active(p, "tariff_charge_windows", n),
    ),
    SensorDef(
        "tariff.export_charge_kwh", "Export-charge Export this period (kWh)", "kWh", "number",
        lambda p, _n: _num(p, "charge_export_kwh"),
    ),
    SensorDef(
        "tariff.export_charge_free_remaining", "Free Export Allowance Remaining (kWh)",
        "kWh", "number", _charge_free_remaining,
    ),
    SensorDef(
        "tariff.export_charge_net_kwh", "Chargeable Export above free (kWh)", "kWh", "number",
        _charge_net_kwh,
    ),
    SensorDef(
        "tariff.export_charge_cost", "Export Charge this period ($)", "$", "number",
        _charge_cost,
    ),
    # ── What the electricity plan permits (utility/plan requirements) ──
    # Referenceable so an automation can refuse to act against its own plan —
    # e.g. only force-discharge while service.export_allowed is true.
    SensorDef(
        "service.export_allowed", "Plan allows export to grid", None, "bool",
        lambda p, _n: bool(p.get("service_export_allowed", True)),
    ),
    SensorDef(
        "service.export_limit_kw", "Plan export limit (kW, 0 = unlimited)", "kW", "number",
        lambda p, _n: _num(p, "service_export_limit_kw"),
    ),
    SensorDef(
        "service.charging_allowed", "Plan allows battery charging", None, "bool",
        lambda p, _n: bool(p.get("service_charging_allowed", True)),
    ),
    SensorDef(
        "service.discharging_allowed", "Plan allows battery discharging", None, "bool",
        lambda p, _n: bool(p.get("service_discharging_allowed", True)),
    ),
    # ── Why the battery is doing what it is ──────────────
    # The scheduler knew which entry owned a target but never said so, leaving
    # "why is it discharging?" answerable only by reading the activity log.
    SensorDef(
        "dispatch.active", "Bridge is overriding the gateway", None, "bool",
        lambda p, _n: bool(p.get("dispatch_active")),
    ),
    SensorDef(
        "dispatch.source",
        "What is driving it: schedule / manual / none (gateway's own mode)",
        None, "enum",
        lambda p, _n: _text(p, "dispatch_source"),
    ),
    SensorDef(
        "dispatch.entry", "Automation currently in control", None, "enum",
        lambda p, _n: _text(p, "dispatch_entry"),
    ),
    SensorDef(
        "dispatch.action", "Action it is holding", None, "enum",
        lambda p, _n: _text(p, "dispatch_action"),
    ),
    SensorDef(
        "dispatch.since_min", "Minutes it has been in control", "min", "number",
        lambda p, _n: _num(p, "dispatch_since_min"),
    ),
    SensorDef(
        "dispatch.expires_min", "Minutes until it releases", "min", "number",
        lambda p, _n: _num(p, "dispatch_expires_min"),
    ),

    # ── Billable vs free import ──────────────────────────
    # "Billable" is a property of the window, not a special free-import
    # feature: a zero-rate wave in the fuller rate model is the same thing, so
    # nothing needs unpicking when that lands.
    SensorDef(
        "tariff.import_window_active", "In a declared grid-import window", None, "bool",
        lambda p, n: _any_window_active(p, "tariff_import_windows", n),
    ),
    SensorDef(
        "tariff.import_billable",
        "Grid import is billable right now (0 = free window)", None, "bool",
        lambda p, n: _import_billable(p, n),
    ),

    # ── Energy cost this period (priced by the rate model) ─
    SensorDef(
        "energy.import_cost", "Grid import cost this period ($)", "$", "number",
        lambda p, _n: _num(p, "energy_import_cost"),
    ),
    SensorDef(
        "energy.export_credit", "Grid export credit this period ($)", "$", "number",
        lambda p, _n: _num(p, "energy_export_credit"),
    ),
    SensorDef(
        "energy.unpriced_import_kwh",
        "Import the plan priced no rate for (kWh) — a coverage gap", "kWh", "number",
        lambda p, _n: _num(p, "energy_unpriced_import_kwh"),
    ),
    SensorDef(
        "energy.unpriced_export_kwh",
        "Export the plan priced no rate for (kWh)", "kWh", "number",
        lambda p, _n: _num(p, "energy_unpriced_export_kwh"),
    ),

    # ── Resolved tariff band (seasons -> blocks -> waves) ─
    SensorDef(
        "tariff.season", "Tariff season in force", None, "enum",
        lambda p, n: _rate_now(p, n)["season"],
    ),
    SensorDef(
        "tariff.wave", "Rate band in force (Off-Peak, Mid-Peak, …)", None, "enum",
        lambda p, n: _rate_now(p, n)["wave_label"],
    ),
    SensorDef(
        "tariff.buy_rate", "Grid import price now ($/kWh)", "$/kWh", "number",
        lambda p, n: _rate_now(p, n)["buy"],
    ),
    SensorDef(
        "tariff.sell_rate", "Grid export price now ($/kWh)", "$/kWh", "number",
        lambda p, n: _rate_now(p, n)["sell"],
    ),

    # ── Where the service is billed (informational) ──
    SensorDef(
        "service.country", "Service country (ISO code)", None, "enum",
        lambda p, _n: _text(p, "service_country"),
    ),
    SensorDef(
        "service.timezone", "Timezone the plan's TOU windows are written in", None, "enum",
        lambda p, _n: _text(p, "service_timezone"),
    ),
    # The point of recording the plan's timezone: compare it to the clock the
    # engine actually evaluates windows on. 0 means the TOU windows are being
    # applied at the wrong wall-clock time. None when the service hasn't stated
    # one — unknown must not read as a failure. Complements time.tz_ok, which
    # catches the clock CHANGING; this catches it never having been right.
    SensorDef(
        "service.tz_matches_clock",
        "Plan timezone matches the clock schedules run on (0 = TOU windows mistimed)",
        None,
        "bool",
        lambda p, _n: _tz_matches_clock(p),
    ),
    # ── Fixed / standing charges (informational; offset target) ──
    SensorDef(
        "fixed.daily_charge", "Fixed Charges per day ($)", "$", "number",
        lambda p, _n: _num(p, "fixed_daily_charge"),
    ),
    SensorDef(
        "fixed.accrued_period", "Fixed Charges accrued this period ($)", "$", "number",
        lambda p, _n: _num(p, "fixed_accrued_period"),
    ),
    SensorDef(
        "fixed.period_total", "Fixed Charges projected this period ($)", "$", "number",
        lambda p, _n: _num(p, "fixed_period_total"),
    ),
    SensorDef(
        "fixed.remaining", "Fixed Charges left to cover this period ($)", "$", "number",
        lambda p, _n: _num(p, "fixed_remaining"),
    ),
    SensorDef(
        "fixed.days_remaining", "Days left in billing period", "days", "number",
        lambda p, _n: _num(p, "fixed_days_remaining"),
    ),
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
    # Every trigger and TOU window is evaluated against the LOCAL clock above.
    # Surface that clock so a wrong container timezone is visible instead of
    # silently shifting every automation (it shifted a daily 18:00 export to
    # 04:00 on 2026-09-12 when a bind mount reverted to UTC).
    SensorDef(
        "time.utc_offset_h",
        "Bridge clock offset from UTC (hours) — wrong value shifts every schedule",
        "h",
        "number",
        lambda _p, n: round(_time.localtime(n.timestamp()).tm_gmtoff / 3600, 2),
    ),
    # 1 while the clock still matches the timezone recorded at install, 0 once
    # it has drifted (every schedule then fires at the wrong wall-clock time).
    # None until the first startup check has run. Referenceable as a condition
    # so an automation can notify on it.
    SensorDef(
        "time.tz_ok",
        "Clock matches the timezone recorded at install (0 = schedules are mistimed)",
        None,
        "bool",
        lambda _p, _n: (lambda ok: None if ok is None else int(ok))(_clock.tz_ok()),
    ),
    # NOTE: no price.* sensor yet — a price source isn't wired. Re-add a
    # ``price.export_c_per_kwh`` SensorDef (mapped to a real point) when the
    # pricing integration lands, rather than shipping a stub that's always None
    # and misleads the condition picker.
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


#: Dropdown group label per sensor-id prefix (for the grouped condition picker).
_GROUP_LABELS = {
    "battery": "Battery",
    "inverter": "Inverter",
    "solar": "Solar / PV",
    "pv": "Solar / PV",
    "grid": "Grid",
    "load": "Load",
    "mode": "Mode",
    "gateway": "Gateway identity",
    "energy": "Energy",
    "const": "Constants",
    "tariff": "Tariff",
    "demand": "Demand / Tariff",
    "bonus": "Demand / Tariff",
    "fixed": "Fixed Charges",
    "service": "Utility Plan",
    "time": "Time",
}


def _group_for(sensor_id: str) -> str:
    return _GROUP_LABELS.get(sensor_id.split(".", 1)[0], "Other")


def sensor_catalog(points: Points | None = None, now: datetime | None = None) -> list[dict]:
    """Registry metadata for the UI dropdowns (``/api/sensors``).

    Each entry carries a ``group`` label so the condition picker can render
    ``<optgroup>``s. When ``points`` is given, each entry also carries its
    current ``value``.
    """
    now = now or datetime.now()
    out = []
    for s in SENSORS:
        row = {
            "id": s.id,
            "label": s.label,
            "unit": s.unit,
            "kind": s.kind,
            "group": _group_for(s.id),
        }
        if points is not None:
            row["value"] = s.getter(points, now)
        out.append(row)
    return out
