"""Period energy totals — today / this week / this month / year-to-date (kWh).

Computed as a **delta of the aGate's lifetime Wh counters** across a period
boundary: `period_total = current_counter − counter_at_period_start`. Cheaper and
more accurate than summing power samples, using the counters the poller already
surfaces (grid_import_wh, grid_export_wh, pv_energy_total_wh,
dc_energy_charged_wh, dc_energy_discharged_wh).

Alignment with FranklinWH Cloud reporting (per the FWHAI investigation): FranklinWH
computes Week/Month/Year boundaries **server-side** (unverified from code), but the
aGate's daily totals reset at **gateway-local midnight** — so `today` here aligns.
`this_week`/`this_month`/`ytd` use calendar defaults (Mon-start / 1st / Jan-1,
local time); exact FranklinWH parity for those is deferred to a future Cloud-API
reconciliation. Set the container TZ to the site's local zone for correct
midnight/boundary math.

Baselines persist (app_config JSON) so period totals survive restarts. Two edges:
- If the Bridge was NOT running at the last boundary, the current partial period's
  baseline is captured on first sample after start → that period is partial until
  the next roll-over (subsequent periods are exact). Persisted baselines cover the
  common case.
- A counter DECREASE (firmware reset / wrap) re-bases that period (never negative).

Stateful (subscribes to the SampleBus); the pure sensor registry reads the
computed totals via points merged in by the app (see main.py `_schedule_points`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from franklinwh_bridge.store.db import get_app_config, set_app_config

logger = logging.getLogger(__name__)

# source id → the lifetime Wh point key it derives from
SOURCES: dict[str, str] = {
    "grid_import": "grid_import_wh",
    "grid_export": "grid_export_wh",
    "solar": "pv_energy_total_wh",
    "battery_charge": "dc_energy_charged_wh",
    "battery_discharge": "dc_energy_discharged_wh",
}
PERIODS: tuple[str, ...] = ("today", "this_week", "this_month", "ytd")
_BASELINES_KEY = "energy_baselines"
_PERSIST_INTERVAL_S = 60


def period_start(period: str, now: datetime) -> datetime:
    """Local-time start of a period containing ``now`` (Mon-start week, calendar
    month/year). ``today`` aligns with the aGate's local-midnight daily reset."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "this_week":
        return day - timedelta(days=now.weekday())  # Monday = 0
    if period == "this_month":
        return day.replace(day=1)
    if period == "ytd":
        return day.replace(month=1, day=1)
    return day  # "today" (and any unknown → today)


class EnergyTotals:
    """Maintains per-gateway/source/period counter baselines and current totals."""

    def __init__(self, db: Any, *, now_fn: Any = None) -> None:
        self._db = db
        self._now = now_fn or datetime.now
        self._last_persist = 0.0
        self._loaded = False
        # gw -> source -> period -> {"baseline": wh, "start": period_start_ts}
        self._base: dict[str, dict[str, dict[str, dict]]] = {}
        # gw -> source -> last-seen lifetime counter (the boundary re-base proxy)
        self._last: dict[str, dict[str, float]] = {}
        # gw -> {point_key: kwh} — current computed totals (merged into points)
        self._totals: dict[str, dict[str, float]] = {}

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        raw = await get_app_config(self._db, _BASELINES_KEY)
        if raw:
            try:
                self._base = json.loads(raw)
            except (ValueError, TypeError):
                self._base = {}
        self._loaded = True

    async def on_sample(self, sample: Any) -> None:
        """SampleBus subscriber. Re-bases at period boundaries / counter resets and
        recomputes the period totals for the gateway."""
        await self._ensure_loaded()
        gw = sample.gateway_id
        points = getattr(sample, "points", None) or {}
        now_dt = self._now()
        gw_base = self._base.setdefault(gw, {})
        gw_last = self._last.setdefault(gw, {})
        totals: dict[str, float] = {}
        changed = False

        for source, key in SOURCES.items():
            cur = points.get(key)
            if isinstance(cur, bool) or not isinstance(cur, (int, float)):
                continue
            cur = float(cur)
            prev = gw_last.get(source)  # last-seen counter ≈ value at a just-crossed boundary
            src_base = gw_base.setdefault(source, {})
            for period in PERIODS:
                start_ts = period_start(period, now_dt).timestamp()
                b = src_base.get(period)
                if b is None or cur < b["baseline"]:
                    # first sample ever, or counter reset (never go negative) → base at cur
                    src_base[period] = {"baseline": cur, "start": start_ts}
                    changed = True
                elif start_ts > b["start"] + 1.0:
                    # crossed into a new period → base at the counter value at the
                    # boundary, approximated by the last pre-boundary reading, so the
                    # sliver between the boundary and this sample isn't dropped.
                    src_base[period] = {
                        "baseline": prev if prev is not None else cur,
                        "start": start_ts,
                    }
                    changed = True
                b = src_base[period]
                totals[f"energy_{source}_{period}_kwh"] = round((cur - b["baseline"]) / 1000.0, 3)
            gw_last[source] = cur

        self._totals[gw] = totals
        if changed or sample.ts - self._last_persist >= _PERSIST_INTERVAL_S:
            self._last_persist = sample.ts
            await self._persist()

    async def _persist(self) -> None:
        try:
            await set_app_config(self._db, _BASELINES_KEY, json.dumps(self._base))
        except Exception as exc:
            logger.debug("EnergyTotals: persist baselines failed: %s", exc)

    def current_totals(self, gw_id: str) -> dict[str, float]:
        """The gateway's current period totals as points (energy_<src>_<period>_kwh
        → kWh), to be merged into the sample points the sensor registry reads."""
        return dict(self._totals.get(gw_id, {}))
