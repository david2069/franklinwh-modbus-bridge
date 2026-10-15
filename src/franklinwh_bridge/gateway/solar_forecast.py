"""Weather + PV solar forecast from Open-Meteo (free, no API key) — #88.

Matches the Local Bridge's ``solar_forecast.py`` so automations port between the
two bridges: same Open-Meteo request, same kW model (GTI/1000 x kWp x 0.85), same
day totals, and the same ``weather.*`` / ``solar_forecast.*`` sensor names.

How it fits this bridge (not the Local Bridge's structure):

* The settings (location + PV array) live in ``app_config`` under one JSON key,
  like :class:`~franklinwh_bridge.gateway.constants.ConstantsStore`. Unset
  latitude/longitude means the feature is off — no fetches, sensors ``None``.
* The scheduler's points merge is **synchronous** and runs every tick, so it can
  never wait on the network. A background loop (:meth:`SolarForecastService.run`)
  keeps the cache warm with ``httpx`` (async) and :meth:`as_points` only reads
  that cache.
* Never raises. A failed fetch returns the last good forecast flagged
  ``stale: True``, or an empty stub carrying ``error``.

Modbus exposes no location register, so there is no "sync from gateway"; the
location is typed in, or read from a configured Home Assistant's ``get_config``.

Open-Meteo is asked for ``timezone=auto``, so hourly times are the SITE's local
wall clock; ``utc_offset_seconds`` aligns "now" to that clock rather than to UTC
or the container's zone.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from franklinwh_bridge.store.db import get_app_config, set_app_config

logger = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
CACHE_S = 900  # 15 minutes
PV_EFFICIENCY = 0.85  # inverter + wiring + soiling derate (GTI -> AC)
TIMEOUT_S = 15.0
APP_CONFIG_KEY = "solar_forecast"

NOT_CONFIGURED_REASON = (
    "Set your location (latitude & longitude) in Settings to enable the solar forecast."
)

# WMO weather codes -> short label (Open-Meteo ``weather_code``). Same table as
# the Local Bridge, so weather.condition compares equal across bridges.
WEATHER_CODES: dict[int, str] = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
    81: "Rain showers", 82: "Violent showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}  # fmt: skip

#: Sensor id -> point key the sensor registry reads. Point keys keep the
#: ``scheduler_sensors`` getters pure (they read points, never this module).
POINT_KEYS: dict[str, str] = {
    "weather.temp_c": "weather_temp_c",
    "weather.condition": "weather_condition",
    "weather.code": "weather_code",
    "solar_forecast.today_kwh": "solar_forecast_today_kwh",
    "solar_forecast.today_peak_kw": "solar_forecast_today_peak_kw",
    "solar_forecast.tomorrow_kwh": "solar_forecast_tomorrow_kwh",
    "solar_forecast.remaining_kwh": "solar_forecast_remaining_kwh",
}


@dataclass
class PvSettings:
    """Location + PV array. Azimuth uses Open-Meteo's convention:
    0 = south, -90 = east, 90 = west, ±180 = north."""

    pv_latitude: float | None = None
    pv_longitude: float | None = None
    pv_kwp: float = 5.0
    pv_tilt: float = 20.0
    pv_azimuth: float = 0.0

    @property
    def configured(self) -> bool:
        return self.pv_latitude is not None and self.pv_longitude is not None

    def cache_key(self) -> str:
        return (
            f"{self.pv_latitude},{self.pv_longitude},{self.pv_tilt},{self.pv_azimuth},{self.pv_kwp}"
        )


# ── Pure model ────────────────────────────────────────────────


def _local_to_epoch(local: str, offset_s: int) -> int | None:
    """Open-Meteo local ``YYYY-MM-DDTHH:MM`` -> epoch seconds, via the site offset."""
    try:
        dt = datetime.strptime(local[:16], "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None
    return calendar.timegm(dt.timetuple()) - int(offset_s or 0)


def compute(raw: dict, kwp: float, *, generated_at: float | None = None) -> dict:
    """Open-Meteo response -> forecast dict (current weather, days, hourly kW)."""
    cur = raw.get("current", {}) or {}
    code = cur.get("weather_code")
    hourly = raw.get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    gti = hourly.get("global_tilted_irradiance", []) or []
    temps = hourly.get("temperature_2m", []) or []
    offset = int(raw.get("utc_offset_seconds") or 0)

    hours = []
    for i, t in enumerate(times):
        g = gti[i] if i < len(gti) and gti[i] is not None else 0
        # GTI (W/m²) at STC 1000 W/m² -> kWp array output, derated.
        kw = round(g / 1000.0 * kwp * PV_EFFICIENCY, 3)
        hours.append(
            {
                "time": t,
                # Epoch seconds of the hour's start, so the UI can render it in the
                # bridge's zone (time_fmt.js) instead of trusting a bare local string.
                "ts": _local_to_epoch(t, offset),
                "kw": kw,
                "temp_c": temps[i] if i < len(temps) else None,
            }
        )

    # Daily totals (hourly kW summed over 1 h steps = kWh).
    by_day: dict[str, list[dict]] = {}
    for h in hours:
        by_day.setdefault(h["time"][:10], []).append(h)
    days = []
    for d in sorted(by_day):
        hs = by_day[d]
        peak = max(hs, key=lambda x: x["kw"])
        days.append(
            {
                "date": d,
                "total_kwh": round(sum(x["kw"] for x in hs), 1),
                "peak_kw": peak["kw"],
                "peak_time": peak["time"][11:16],
                "peak_ts": peak["ts"],
            }
        )

    return {
        "current": {
            "temp_c": cur.get("temperature_2m"),
            "code": code,
            "condition": WEATHER_CODES.get(code, "—") if code is not None else "—",
        },
        "days": days,
        "hourly": hours,
        "utc_offset_seconds": raw.get("utc_offset_seconds"),
        "timezone": raw.get("timezone"),
        "generated_at": int(generated_at if generated_at is not None else time.time()),
    }


def empty_forecast(error: str) -> dict:
    return {
        "current": {"temp_c": None, "condition": "—", "code": None},
        "days": [],
        "hourly": [],
        "error": error,
    }


def _site_now(fc: dict, now_ts: float) -> datetime:
    """Naive datetime on the SITE's wall clock (Open-Meteo hourly times are local)."""
    off = int((fc or {}).get("utc_offset_seconds") or 0)
    return datetime.fromtimestamp(now_ts, UTC).replace(tzinfo=None) + timedelta(seconds=off)


def _day(fc: dict, date: str) -> dict:
    for d in (fc or {}).get("days", []) or []:
        if d.get("date") == date:
            return d
    return {}


def sensors(fc: dict | None, now_ts: float | None = None) -> dict[str, Any]:
    """Flatten a forecast into the ``weather.*`` / ``solar_forecast.*`` namespace.

    Missing data -> None, which fails a condition closed. "Today" is the site's
    local date at ``now_ts`` (not simply the first day returned), so a forecast
    that outlived its day reports None rather than yesterday's figures.
    """
    fc = fc or {}
    now_ts = time.time() if now_ts is None else now_ts
    cur = fc.get("current", {}) or {}
    hourly = fc.get("hourly", []) or []
    site_now = _site_now(fc, now_ts)
    today_date = site_now.strftime("%Y-%m-%d")
    tomorrow_date = (site_now + timedelta(days=1)).strftime("%Y-%m-%d")
    today = _day(fc, today_date)
    tomorrow = _day(fc, tomorrow_date)
    # Remaining production today = hourly kW from the current hour on, for
    # today's date. Same rule as the Local Bridge: an hour counts if its start
    # is at or after "now" on the site clock.
    remaining = None
    if today:
        now_s = site_now.strftime("%Y-%m-%dT%H:%M")
        remaining = round(
            sum(h["kw"] for h in hourly if h["time"][:10] == today_date and h["time"] >= now_s),
            1,
        )
    return {
        "weather.temp_c": cur.get("temp_c"),
        "weather.condition": cur.get("condition") if cur.get("code") is not None else None,
        "weather.code": cur.get("code"),
        "solar_forecast.today_kwh": today.get("total_kwh"),
        "solar_forecast.today_peak_kw": today.get("peak_kw"),
        "solar_forecast.remaining_kwh": remaining,
        "solar_forecast.tomorrow_kwh": tomorrow.get("total_kwh"),
    }


def actual_vs_forecast(
    actual_kwh: float | None, today_kwh: float | None, remaining_kwh: float | None
) -> dict[str, float | None]:
    """``day_pct`` (actual / today's forecast) and ``vs_expected_pct`` (actual /
    forecast-so-far, where so-far = today - remaining). Same arithmetic as the
    Local Bridge's scheduler; None whenever an input is missing or the
    denominator isn't positive."""
    day_pct = None
    if actual_kwh is not None and today_kwh is not None and today_kwh > 0:
        day_pct = round(actual_kwh / today_kwh * 100, 1)
    vs = None
    if actual_kwh is not None and today_kwh is not None and remaining_kwh is not None:
        expected = today_kwh - remaining_kwh
        if expected > 0:
            vs = round(actual_kwh / expected * 100, 1)
    return {"solar_forecast.day_pct": day_pct, "solar_forecast.vs_expected_pct": vs}


# ── Fetch ─────────────────────────────────────────────────────

Fetcher = Callable[[PvSettings], Awaitable[dict]]


async def fetch_open_meteo(s: PvSettings) -> dict:
    """One Open-Meteo request (raises on any failure — the caller handles it)."""
    params = {
        "latitude": s.pv_latitude,
        "longitude": s.pv_longitude,
        "timezone": "auto",
        "forecast_days": 2,
        "current": "temperature_2m,weather_code",
        "hourly": "temperature_2m,global_tilted_irradiance",
        "tilt": s.pv_tilt,
        "azimuth": s.pv_azimuth,
    }
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S, headers={"User-Agent": "franklinwh-modbus-bridge"}
    ) as client:
        r = await client.get(OPEN_METEO, params=params)
        r.raise_for_status()
        return r.json()


# ── Service ───────────────────────────────────────────────────


def _validate(key: str, v: Any) -> float | None:
    if v is None:
        if key in ("pv_latitude", "pv_longitude"):
            return None
        raise ValueError(f"{key} cannot be empty")
    fv = float(v)
    bounds = {
        "pv_latitude": (-90.0, 90.0),
        "pv_longitude": (-180.0, 180.0),
        "pv_kwp": (0.0, 1000.0),
        "pv_tilt": (0.0, 90.0),
        "pv_azimuth": (-180.0, 180.0),
    }[key]
    if not bounds[0] <= fv <= bounds[1]:
        raise ValueError(f"{key} must be between {bounds[0]:g} and {bounds[1]:g}")
    return fv


class SolarForecastService:
    """Holds the PV settings + forecast cache; keeps it warm in the background."""

    def __init__(self, db: Any, *, fetcher: Fetcher | None = None) -> None:
        self._db = db
        self._fetch = fetcher or fetch_open_meteo
        self.settings = PvSettings()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    # ── settings ──
    async def load(self) -> None:
        try:
            raw = await get_app_config(self._db, APP_CONFIG_KEY)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Solar forecast settings load failed: %s", exc)
            return
        if not raw:
            return
        try:
            stored = json.loads(raw)
        except (ValueError, TypeError):
            return
        for k in asdict(self.settings):
            if k in stored:
                with contextlib.suppress(TypeError, ValueError):
                    setattr(self.settings, k, _validate(k, stored[k]))

    def values(self) -> dict[str, Any]:
        return asdict(self.settings)

    async def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply + persist known keys. ``None`` clears latitude/longitude (= off).
        Invalid values raise ValueError and nothing is changed."""
        fields = asdict(self.settings)
        validated = {k: _validate(k, v) for k, v in updates.items() if k in fields}
        for k, v in validated.items():
            setattr(self.settings, k, v)
        await set_app_config(self._db, APP_CONFIG_KEY, json.dumps(self.values()))
        self._wake.set()  # fetch for the new location now, not in 15 minutes
        return self.values()

    # ── forecast ──
    async def forecast(self, *, force: bool = False) -> dict:
        """Weather + PV forecast for the configured site. Cached 15 min per
        lat/lon/tilt/azimuth/kWp; on failure the last good result flagged
        ``stale``, or an empty stub with ``error``. Never raises."""
        s = PvSettings(**self.values())
        if not s.configured:
            return empty_forecast(NOT_CONFIGURED_REASON)
        key = s.cache_key()
        async with self._lock:
            now = time.time()
            hit = self._cache.get(key)
            if not force and hit and now - hit[0] < CACHE_S:
                return hit[1]
            try:
                out = compute(await self._fetch(s), s.pv_kwp, generated_at=now)
                self._cache = {key: (now, out)}  # one site: drop other keys
                return out
            except Exception as exc:
                logger.warning("Solar forecast fetch failed: %s", exc)
                if hit:
                    return {**hit[1], "stale": True}
                return empty_forecast(str(exc) or type(exc).__name__)

    def cached(self) -> dict | None:
        """The last good forecast for the current settings, however old."""
        if not self.settings.configured:
            return None
        hit = self._cache.get(self.settings.cache_key())
        return hit[1] if hit else None

    def as_points(self, now_ts: float | None = None) -> dict[str, Any]:
        """Sensor values as point keys for the (sync) scheduler points merge.
        Cache only — never a network call. All None when off or not fetched."""
        vals = sensors(self.cached(), now_ts) if self.cached() else dict.fromkeys(POINT_KEYS)
        return {POINT_KEYS[k]: vals.get(k) for k in POINT_KEYS}

    async def run(self) -> None:
        """Keep the cache warm while configured. Cancel the task to stop."""
        while True:
            if self.settings.configured:
                try:
                    await self.forecast()
                except Exception as exc:  # pragma: no cover - forecast() never raises
                    logger.debug("Solar forecast refresh failed: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), CACHE_S)
            self._wake.clear()
