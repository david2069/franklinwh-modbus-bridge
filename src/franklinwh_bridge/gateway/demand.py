"""Demand-charge + battery-bonus calculator.

Tracks, over the current **billing period** (from the service's configurable
billing-cycle day), the peak grid-import demand and the bonus-window export:

- **Peak demand (kW)** = the highest completed grid-import interval (kWh) that
  falls inside the demand window, × (60/interval_min) — 30-min→×2, 15→×4, 60→×1.
  The interval + charge basis are user-configurable (per service).
- **Demand charge** = peak_kW × demand_rate ($/kW/day) × days in the period.
- **Bonus export (kWh)** = grid export accumulated while inside the bonus window
  (v1: total grid export — may include solar, flagged in the UI).
- **Bonus credit** = bonus_kWh × export_bonus_rate ($/kWh).

Uses the monotonic ``grid_import_wh`` / ``grid_export_wh`` counters (default
gateway) via counter deltas, mirroring EnergyTotals. State persists to
``app_config`` so a restart doesn't lose the period's running peak. The window +
rate + cycle day come from BillingStore (first service with each flag).
"""

from __future__ import annotations

import calendar
import json
import logging
from datetime import datetime
from typing import Any

from franklinwh_bridge.gateway.scheduler_sensors import _in_window
from franklinwh_bridge.store.db import get_app_config, set_app_config

logger = logging.getLogger(__name__)

_KEY = "demand_state"
_PERSIST_INTERVAL_S = 60
_INTERVAL_MIN = 30  # NEM demand interval


def _period_start(now: datetime, cycle_day: int) -> datetime:
    """Start of the billing period containing ``now`` for a monthly cycle whose
    boundary is ``cycle_day`` (clamped to month length). The most recent
    occurrence of the cycle day at/before ``now``."""
    cycle_day = max(1, min(31, cycle_day))
    day = min(cycle_day, calendar.monthrange(now.year, now.month)[1])
    start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if start > now:  # cycle day later this month → step to previous month
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        start = start.replace(year=y, month=m, day=min(cycle_day, calendar.monthrange(y, m)[1]))
    return start


def _period_end(period_start: datetime, cycle_day: int) -> datetime:
    """Start of the NEXT period (one month after period_start, day-clamped)."""
    y, m = (period_start.year, period_start.month + 1)
    if m > 12:
        y, m = y + 1, 1
    return period_start.replace(year=y, month=m, day=min(cycle_day, calendar.monthrange(y, m)[1]))


class DemandTracker:
    def __init__(self, db: Any, billing: Any, *, gateway_id: str = "default") -> None:
        self._db = db
        self._billing = billing
        self._gw = gateway_id
        self._last_persist = 0.0
        self._s: dict[str, Any] = self._blank()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {
            "period_start": None,        # unix ts of the current period start
            "interval_start": None,      # unix ts of the current 30-min interval
            "interval_start_import_wh": None,
            "last_import_wh": None,
            "last_export_wh": None,
            "peak_kwh": 0.0,             # max completed-interval import kWh in window
            "cur_interval_kwh": 0.0,     # accumulated import this interval (any window)
            "bonus_export_wh": 0.0,      # export accumulated inside the bonus (reward) window
            "charge_export_wh": 0.0,     # export accumulated inside the export-charge window
        }

    async def load(self) -> None:
        try:
            raw = await get_app_config(self._db, _KEY)
        except Exception as exc:  # pragma: no cover
            logger.debug("DemandTracker load failed: %s", exc)
            return
        if raw:
            try:
                stored = json.loads(raw)
            except (ValueError, TypeError):
                return
            self._s.update({k: stored.get(k, v) for k, v in self._blank().items()})

    def _cycle_day(self) -> int:
        cfg = self._billing.demand_config() or self._billing.bonus_config() or {}
        return int(cfg.get("cycle_day", 1) or 1)

    def _interval_min(self) -> int:
        cfg = self._billing.demand_config() or {}
        iv = int(cfg.get("interval_min", _INTERVAL_MIN) or _INTERVAL_MIN)
        return iv if iv in (15, 30, 60) else _INTERVAL_MIN

    async def on_sample(self, sample: Any) -> None:
        if getattr(sample, "gateway_id", None) != self._gw:
            return
        pts = sample.points or {}
        imp = pts.get("grid_import_wh")
        exp = pts.get("grid_export_wh")
        now = datetime.fromtimestamp(sample.ts)
        cycle_day = self._cycle_day()

        # ── period rollover ─────────────────────────────────────
        ps = _period_start(now, cycle_day)
        if self._s["period_start"] != ps.timestamp():
            self._s.update(self._blank())
            self._s["period_start"] = ps.timestamp()

        # ── demand: 30-min interval buckets ─────────────────────
        if isinstance(imp, (int, float)):
            imp = float(imp)
            interval_min = self._interval_min()
            slot = now.replace(
                minute=(now.minute // interval_min) * interval_min, second=0, microsecond=0
            )
            st = self._s["interval_start"]
            last = self._s["last_import_wh"]
            counter_reset = last is not None and imp < last
            if st is None or self._s["interval_start_import_wh"] is None or counter_reset:
                self._start_interval(slot, imp)
            elif slot.timestamp() != st:
                # A new interval started → close the previous one. Its energy is
                # the counter delta up to NOW (the boundary): current imp minus
                # the counter at the interval's start — not `last` (which may be a
                # mid-interval reading). Fold into the period peak if in-window.
                prev_kwh = max(0.0, (imp - self._s["interval_start_import_wh"]) / 1000.0)
                dcfg = self._billing.demand_config()
                if dcfg and _in_window(dcfg["window"], datetime.fromtimestamp(st)):
                    self._s["peak_kwh"] = max(self._s["peak_kwh"], prev_kwh)
                self._start_interval(slot, imp)
            # live accumulation this interval
            base = self._s["interval_start_import_wh"]
            self._s["cur_interval_kwh"] = (
                max(0.0, (imp - base) / 1000.0) if base is not None else 0.0
            )
            self._s["last_import_wh"] = imp

        # ── export accumulation inside the reward + charge windows ───
        if isinstance(exp, (int, float)):
            exp = float(exp)
            last = self._s["last_export_wh"]
            if last is not None and exp >= last:  # monotonic counter delta
                delta = exp - last
                bcfg = self._billing.bonus_config()
                if bcfg and _in_window(bcfg["window"], now):
                    self._s["bonus_export_wh"] += delta
                ccfg = self._billing.charge_config()
                if ccfg and _in_window(ccfg["window"], now):
                    self._s["charge_export_wh"] += delta
            self._s["last_export_wh"] = exp

        if sample.ts - self._last_persist >= _PERSIST_INTERVAL_S:
            self._last_persist = sample.ts
            await self._persist()

    def _start_interval(self, slot: datetime, imp: float) -> None:
        self._s["interval_start"] = slot.timestamp()
        self._s["interval_start_import_wh"] = imp
        self._s["cur_interval_kwh"] = 0.0

    async def _persist(self) -> None:
        try:
            await set_app_config(self._db, _KEY, json.dumps(self._s))
        except Exception as exc:  # pragma: no cover
            logger.debug("DemandTracker persist failed: %s", exc)

    def as_points(self, now: datetime | None = None) -> dict[str, float]:
        """Calculator outputs merged into the sensor snapshot. The sensors
        (scheduler_sensors) turn these into demand.*/bonus.* values."""
        now = now or datetime.now()
        # interval kWh → kW: multiply by (60 / interval_min). 30→×2, 15→×4, 60→×1.
        mult = 60.0 / self._interval_min()
        peak_kw = round(self._s["peak_kwh"] * mult, 3)
        # live current-interval running-average kW (accumulated ÷ elapsed hours)
        interval_kw = 0.0
        st = self._s["interval_start"]
        if st is not None:
            elapsed_h = max(1.0 / 3600.0, (now.timestamp() - st) / 3600.0)
            interval_kw = round(self._s["cur_interval_kwh"] / elapsed_h, 3)
        ps_ts = self._s["period_start"]
        days = 0.0                # days elapsed this period
        period_days = 0.0         # full length of the billing period (for the free allowance)
        if ps_ts is not None:
            ps = datetime.fromtimestamp(ps_ts)
            days = round(max(0.0, (now - ps).total_seconds() / 86400.0), 3)
            pe = _period_end(ps, self._cycle_day())
            period_days = round((pe - ps).total_seconds() / 86400.0, 3)
        # Export-charge free allowance = free_kwh_per_day × full-period days.
        ccfg = self._billing.charge_config() or {}
        free_kwh = round(float(ccfg.get("free_kwh_per_day") or 0.0) * period_days, 3)
        return {
            "demand_peak_kw": peak_kw,
            "demand_interval_kw": interval_kw,
            "demand_days_in_period": days,
            "demand_period_days": period_days,
            "bonus_export_kwh": round(self._s["bonus_export_wh"] / 1000.0, 3),
            "charge_export_kwh": round(self._s["charge_export_wh"] / 1000.0, 3),
            "charge_free_kwh": free_kwh,
        }
