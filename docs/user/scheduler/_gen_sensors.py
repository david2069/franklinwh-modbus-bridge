"""Regenerate the condition-sensor tables in conditions.md from the live registry.

Run from the repository root with the project venv active:

    python docs/user/scheduler/_gen_sensors.py

It imports ``franklinwh_bridge.gateway.scheduler_sensors`` (no bridge needs to be
running), groups the sensors exactly as the condition picker does, and rewrites
the block between the two ``<!-- sensors:begin -->`` / ``<!-- sensors:end -->``
markers in ``conditions.md``. Pass ``--print`` to write to stdout instead.

The id, label, unit, type and group come from the code. The "Needs", "Without
it" and "Refreshes" columns come from the DEPS table below, which is written by
hand from the code that fills each point (poller, ConstantsStore, BillingStore,
DemandTracker, FixedChargesStore, EnergyTotals, SolarForecastService,
NemPricingService, the schedule engine). Keep DEPS in step when a sensor is
added: an id with no DEPS entry is printed with "?" so the gap is visible.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog

PAGE = Path(__file__).with_name("conditions.md")
BEGIN = "<!-- sensors:begin -->"
END = "<!-- sensors:end -->"

POLL = "every poll (default 10 s)"
TICK = "every tick (15 s)"

# Shorthand for the settings pages, so every row names the same place.
CONST = "Settings → Energy & Automation → Automation Constants"
SERVICE = "Settings → Energy & Automation → Electricity Utility Service(s)"
LOCATION = "Settings → Site & Gateways → Location & Solar"
SPOT = "Settings → Site & Gateways → Wholesale spot price"

# (needs, without it, refreshes). Matched by exact id first, then by the
# longest id prefix.
DEPS: dict[str, tuple[str, str, str]] = {
    # live poll
    "battery.soc_pct": ("Gateway polling", "None", POLL),
    "battery.reserve_pct": ("Gateway polling (15508 / 15509)", "None", POLL),
    "battery.reserve_self_pct": (
        "Self-Consumption must be the active mode", "None in any other mode", POLL),
    "battery.reserve_tou_pct": ("TOU must be the active mode", "None in any other mode", POLL),
    "battery.power_w": ("Gateway polling. + = discharging, − = charging", "None", POLL),
    "battery.status": ("Gateway polling", "None", POLL),
    "solar.power_w": ("Gateway polling", "None", POLL),
    "pv.is_generating": ("Gateway polling. True above 50 W of solar", "None", POLL),
    "grid.power_w": ("Gateway polling. + = importing, − = exporting", "None", POLL),
    "grid.connected": ("Gateway polling (connection state, else grid mode)", "None", POLL),
    "grid.status": ("Gateway polling", "None", POLL),
    "load.power_w": ("Gateway polling", "None", POLL),
    "mode.name": ("Gateway polling", "None", POLL),
    "mode.raw": ("Gateway polling", "None", POLL),
    "inverter.status": ("Gateway polling", "None", POLL),
    # identity
    "gateway.device_type": (
        "Gateway config (not polled)", "\"agate\"", "on config change"),
    "gateway.battery_capable": (
        "Gateway config: False for a MAC-1 collar", "True", "on config change"),
    "gateway.": ("Nameplate (SunSpec Model 1) read by the poller", "None", POLL),
    # derived
    "battery.capacity_kwh": ("Battery nameplate 713.WHRtg", "None", POLL),
    "battery.stored_kwh": ("Capacity + SoC", "None", POLL),
    "battery.remaining_kwh": ("Capacity + SoC", "None", POLL),
    "inverter.power_rating_w": (
        "Model 702 nameplate rating (not the trusted battery rating)", "None", POLL),
    "inverter.utilised_w": ("Battery power", "None", POLL),
    "inverter.unutilised_w": ("702 nameplate rating + battery power", "None", POLL),
    "battery.time_to_charge_min": (
        "Capacity, SoC, const Max Charge SoC, 702 max charge rate", "None", POLL),
    "battery.time_to_discharge_min": (
        "Capacity, SoC, const Min Discharge SoC, 702 max discharge rate", "None", POLL),
    "battery.time_to_charge_now_min": (
        "As above, plus the battery must be charging now", "None when idle or discharging", POLL),
    "battery.time_to_discharge_now_min": (
        "As above, plus the battery must be discharging now", "None when idle or charging", POLL),
    # constants
    "const.": (CONST, "Never missing: defaults 20 / 100 / 30 %", "on save"),
    # tariff windows and calculators
    "tariff.demand_window_active": (
        f"A peak-demand window on a service ({SERVICE})", "False", TICK),
    "tariff.bonus_window_active": (
        f"An export-bonus window on a service ({SERVICE})", "False", TICK),
    "tariff.export_charge_window_active": (
        f"An export-charge window on a service ({SERVICE})", "False", TICK),
    "tariff.import_window_active": (f"Import windows on a service ({SERVICE})", "False", TICK),
    "tariff.import_billable": (
        f"Energy rates or import windows ({SERVICE})", "None", TICK),
    "tariff.export_charge_": (
        f"Export charge on a service ({SERVICE}); counts the default gateway", "0", POLL),
    "tariff.tier": (f"Energy rates with tiers ({SERVICE})", "None", TICK),
    "tariff.season": (f"Energy rates with seasons ({SERVICE})", "None", TICK),
    "tariff.time_period": (f"Energy rates ({SERVICE})", "None", TICK),
    "tariff.buy_rate": (f"Energy rates, or a dynamic plan ({SERVICE})", "None", TICK),
    "tariff.sell_rate": (f"Energy rates, or a dynamic plan ({SERVICE})", "None", TICK),
    "tariff.spot_price": (
        f"A NEM region or an HA price entity ({SPOT}), or a dynamic plan",
        "None (also None when the last price is over 30 min old)", "every 240 s"),
    "tariff.feed_in_price": (
        f"A NEM region or an HA feed-in entity ({SPOT}), or a dynamic plan",
        "None (also None when the last price is over 30 min old)", "every 240 s"),
    "demand.": (
        f"Peak demand on a service ({SERVICE}); measures the default gateway", "0", POLL),
    "bonus.": (
        f"Export bonus on a service ({SERVICE}); measures the default gateway", "0", POLL),
    "service.pto_approved": (f"PTO status on the service ({SERVICE})", "None", "on save"),
    "service.tz_matches_clock": (f"Timezone on the service ({SERVICE})", "None", TICK),
    "service.country": (f"Country on the service ({SERVICE})", "None", "on save"),
    "service.timezone": (f"Timezone on the service ({SERVICE})", "None", "on save"),
    "service.pto_status": (f"PTO status on the service ({SERVICE})", "\"unknown\"", "on save"),
    "service.export_limit_kw": (f"Plan terms on the service ({SERVICE})", "0 (unlimited)", "on save"),
    "service.": (f"Plan terms on the service ({SERVICE})", "True (permissive)", "on save"),
    "fixed.": (f"Standing charges on a service ({SERVICE})", "0", TICK),
    "energy.period_import_kwh": (
        "Billing period of the service; measures the default gateway", "0", POLL),
    "energy.import_cost": (f"Energy rates ({SERVICE}); default gateway", "0", POLL),
    "energy.export_credit": (f"Energy rates ({SERVICE}); default gateway", "0", POLL),
    "energy.unpriced_": (f"Energy rates ({SERVICE}); default gateway", "0", POLL),
    "energy.": ("Gateway's lifetime energy counters (Modbus)", "None until counters are read",
                POLL),
    # engine
    "dispatch.active": ("Schedule engine (or a live WSet from manual control)", "False", TICK),
    "dispatch.source": ("Schedule engine", "\"none\"", TICK),
    "dispatch.": ("A schedule entry owning the gateway", "None", TICK),
    # forecasts
    "weather.": (f"Latitude and longitude ({LOCATION})", "None", "every 15 min"),
    "solar_forecast.day_pct": (
        f"Location ({LOCATION}) + today's solar energy total", "None", "every 15 min"),
    "solar_forecast.vs_expected_pct": (
        f"Location ({LOCATION}) + today's solar energy total", "None", "every 15 min"),
    "solar_forecast.": (
        f"Location; array kWp, tilt, azimuth ({LOCATION})", "None", "every 15 min"),
    # clock
    "time.tz_ok": ("Startup timezone check", "None until the first check", "at startup"),
    "time.": ("Bridge clock", "Never missing", TICK),
}

# Period energy totals override the lifetime "energy." default above.
_PERIOD = re.compile(r"^energy\.[a-z_]+\.(today|this_week|this_month|ytd)_kwh$")


def deps_for(sid: str) -> tuple[str, str, str]:
    if _PERIOD.match(sid):
        return ("Energy totals from the gateway's counters (per gateway)",
                "None until the first counter delta", POLL)
    if sid in DEPS:
        return DEPS[sid]
    best = max((k for k in DEPS if k.endswith((".", "_")) and sid.startswith(k)),
               key=len, default=None)
    return DEPS[best] if best else ("?", "?", "?")


def _cell(s: object) -> str:
    return str(s).replace("|", "\\|")


def render() -> str:
    groups: dict[str, list[dict]] = {}
    for row in sensor_catalog():
        groups.setdefault(row["group"], []).append(row)
    out: list[str] = []
    for label, rows in groups.items():
        out.append(f"### {label}\n")
        out.append("| Picker label | Sensor id | Unit | Type | Needs | Without it | Refreshes |")
        out.append("|---|---|---|---|---|---|---|")
        for r in rows:
            needs, missing, refresh = deps_for(r["id"])
            out.append(
                f"| {_cell(r['label'])} | `{r['id']}` | {_cell(r['unit'] or '—')} "
                f"| {r['kind']} | {_cell(needs)} | {_cell(missing)} | {_cell(refresh)} |"
            )
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    table = render()
    if "--print" in sys.argv:
        sys.stdout.write(table)
        return
    text = PAGE.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        sys.exit(f"markers {BEGIN} / {END} not found in {PAGE}")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    PAGE.write_text(f"{head}{BEGIN}\n\n{table}\n{END}{tail}", encoding="utf-8")
    print(f"updated {PAGE}")


if __name__ == "__main__":
    main()
