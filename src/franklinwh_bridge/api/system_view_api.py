"""The System view (DC bridge #428): one picture of the site for the shared
franklinwh-system-view overlay.

``GET /api/system`` builds the package's payload from each gateway's cached
sample: no Modbus I/O, so opening the view sends nothing to an aGate. Values
are as read; anything the bridge can't read over Modbus is left out or named
in ``unavailable`` (smart circuits and the generator have no Modbus points).
The site's name and weather come from the solar forecast's cache (no fetch
while a request waits), and the user's Customise diagram answers, saved by
``PUT /api/system/customise``, are applied last (as the DC bridge, #428).

Signs follow the points: ``grid_power_w`` > 0 imports, ``battery_power_w`` > 0
discharges, the same as the package.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite
import franklinwh_system_view as sv
from fastapi import APIRouter, HTTPException, Request

from franklinwh_bridge.store.db import (
    gateway_is_unconfigured,
    get_app_config,
    get_gateways,
    set_app_config,
)

router = APIRouter(prefix="/api", tags=["system-view"])

CUSTOMISE_KEY = "system_view_customise"  # app_config: the Customise answers, as JSON
INTRO = ("Where no reading tells the bridge how your site is built, tell it here. Empty or "
         "Automatic uses the readings. Saved in the bridge, so every browser shows the same "
         "diagram.")

W = 50  # below this, a solar input counts as not producing (meter noise)

# Solar inputs as the poller names them (W), and where each is wired.
_PV_INPUTS = (("pv_proximal", "agate", "Solar (aGate)"),
              ("pv_remote1", "apbox", "Remote solar 1"),
              ("pv_remote2", "apbox", "Remote solar 2"))

# Inputs seen producing since the bridge started, per gateway, so an array
# stays drawn at night once it has been seen.
_seen_pv: dict[str, set[str]] = {}


def _n(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _grid_state(pts: dict) -> str:
    """M701 ConnSt, as the aggregator reads it; DERMode only when ConnSt is missing,
    as the scheduler's grid sensor does."""
    conn = str(pts.get("connection_state") or "").lower()
    if "disconnect" in conn:
        return "outage"
    if conn == "connected":
        return "connected"
    if not conn and pts.get("grid_mode"):
        return "outage" if pts["grid_mode"] == "Grid Forming" else "connected"
    return "unknown"


def _legs(pts: dict, ac_type: Any) -> list[float] | None:
    keys = {1: ("voltage_l1_v", "voltage_l2_v"),
            2: ("voltage_l1_v", "voltage_l2_v", "voltage_l3_v")}.get(ac_type)
    if keys is None:
        return None
    legs = [_n(pts.get(k)) for k in keys]
    return legs if all(v is not None for v in legs) else None


def _solar(gw_id: str, pts: dict) -> list[dict]:
    seen = _seen_pv.setdefault(gw_id, set())
    arrays = []
    for key, where, name in _PV_INPUTS:
        p = _n(pts.get(key))
        if p is None:
            continue
        if p >= W:
            seen.add(key)
        if key in seen:
            arrays.append({"id": key, "where": where, "name": name, "power_w": p})
    if arrays:
        return arrays
    if any(k in pts for k, _, _ in _PV_INPUTS):
        # Vendor PV read: nothing producing now or since start. A site with
        # lifetime PV energy still has solar; draw it, idle.
        if (_n(pts.get("pv_energy_total_wh")) or 0) > 0:
            return [{"id": "solar", "where": "agate", "power_w": _n(pts.get("total_solar"))}]
        return []
    # No vendor PV values: M502's AC output is the only solar reading.
    ac = _n(pts.get("ac_power_w"))
    if ac is not None and ac >= W:
        seen.add("ac_power_w")
    return [{"id": "solar", "where": "agate", "power_w": ac}] if "ac_power_w" in seen else []


def _battery(pts: dict, port_count: int | None) -> dict:
    soc = _n(pts.get("soc"))
    bat: dict[str, Any] = {
        "soc_pct": max(0.0, min(100.0, soc)) if soc is not None else None,
        "power_w": _n(pts.get("battery_power_w")),
        "count": port_count,
    }
    if port_count and port_count > 1:
        units = [{"id": f"bat{i}", "name": f"aPower {i}",
                  "power_w": _n(pts.get(f"battery_{i}_power_w"))}
                 for i in range(1, port_count + 1)]
        if any(u["power_w"] is not None for u in units):
            bat["units"] = units
    return bat


def gateway_payload(row: dict, inst: Any) -> dict:
    """One gateway's entry, from its DB row and its running instance (or None)."""
    gw_id, name = row["id"], row.get("name") or row["id"]
    last = inst.sample_bus.last_sample if inst is not None else None
    online = bool(inst is not None and inst.status.connected
                  and last is not None and last.quality != "error")
    base = {"id": gw_id, "name": name, "kind": "agate", "online": online,
            "circuits": [], "unavailable": ["circuits", "generator"]}
    if not online:
        return {**base, "mode": None, "inverter": None,
                "grid": {"state": "unknown", "power_w": None}, "home": {"power_w": None},
                "battery": {"soc_pct": None, "power_w": None}, "solar": []}
    pts = dict(last.points)
    port_count = pts.get("battery_port_count")
    if not isinstance(port_count, int):
        port_count = getattr(inst.poller, "battery_port_count", None) if inst.poller else None
    grid: dict[str, Any] = {"state": _grid_state(pts), "power_w": _n(pts.get("grid_power_w")),
                            "voltage_v": _n(pts.get("voltage_v")),
                            "freq_hz": _n(pts.get("frequency_hz"))}
    legs = _legs(pts, pts.get("ac_type_code", inst.status.ac_type))
    if legs:
        grid["legs_v"] = legs
    return {**base,
            "mode": pts.get("mode_name"), "inverter": pts.get("inverter_state"),
            "reserve_pct": _n(pts.get("active_reserve_pct")),
            "grid": grid,
            "home": {"power_w": _n(pts.get("home_load_ext"))},
            "battery": _battery(pts, port_count),
            "solar": _solar(gw_id, pts)}


def place(tz: Any) -> str | None:
    """A timezone's place: ``Australia/Sydney`` → ``Sydney``."""
    if not isinstance(tz, str) or "/" not in tz:
        return None
    return tz.rsplit("/", 1)[1].replace("_", " ")


def site_payload(forecast: dict | None) -> dict:
    """The site from the solar forecast's cache: its place and the weather now.
    No forecast location set, or none fetched yet: no name and no weather."""
    out: dict[str, Any] = {}
    if not forecast:
        return out
    name = place(forecast.get("timezone"))
    if name:
        out["name"], out["timezone"] = name, forecast["timezone"]
    cur = forecast.get("current") or {}
    if isinstance(cur.get("code"), int) and not isinstance(cur.get("code"), bool):
        out["weather"] = {"code": cur["code"], "condition": cur.get("condition"),
                          "temp_c": _n(cur.get("temp_c"))}
    return out


async def raw_payload(request: Request) -> dict:
    """Every set-up gateway, default first, from readings alone (before the
    user's answers). Disabled and unconfigured gateways are left out; a stopped
    one is drawn offline."""
    db: aiosqlite.Connection = request.app.state.db
    registry = getattr(request.app.state, "registry", None)
    fc = getattr(request.app.state, "solar_forecast", None)
    gateways = [gateway_payload(row, registry.get(row["id"]) if registry else None)
                for row in await get_gateways(db)
                if row.get("enabled") and not gateway_is_unconfigured(row)]
    return {"version": sv.SCHEMA_VERSION,
            "site": site_payload(fc.cached() if fc is not None else None),
            "gateways": gateways}


async def load_saved(db: aiosqlite.Connection) -> dict:
    """The Customise answers; none saved, or unreadable, is no answers."""
    try:
        data = json.loads(await get_app_config(db, CUSTOMISE_KEY) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/system")
async def get_system(request: Request) -> dict:
    """The System view payload, with the Customise answers applied, both layouts
    and the Customise diagram form."""
    raw = await raw_payload(request)
    saved = await load_saved(request.app.state.db)
    out = sv.customise.apply(raw, saved)
    out["customise"] = {"endpoint": "api/system/customise", "intro": INTRO,
                        "fields": sv.customise.fields(raw, saved)}
    return sv.build(out)


@router.put("/system/customise")
async def put_system_customise(request: Request) -> dict:
    """Save the Customise diagram answers. Bridge settings only: nothing is sent
    to an aGate. Replaces the saved answers; an empty answer means Automatic."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expected a JSON object") from exc
    try:
        values = sv.customise.clean(body, await raw_payload(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await set_app_config(request.app.state.db, CUSTOMISE_KEY, json.dumps(values))
    return {"ok": True, "saved": values}
