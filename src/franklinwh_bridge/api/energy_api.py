"""Energy flow REST endpoints — the Sankey's data, and the lifetime counters.

Two different kinds of number live here, and the distinction matters when the
two disagree on screen:

* ``totals`` are **measured** — deltas of the aGate's own lifetime Wh counters,
  the same figures :mod:`energy_totals` feeds the automation sensors.
* ``flows`` are **reconstructed** — directional arcs derived from the power
  series by :mod:`energy_flow`, because no counter reports them.

The response carries both, plus ``residual_kwh`` and ``covered_s``, so a caller
can tell a quiet data gap from a genuinely quiet day.
"""

from __future__ import annotations

import datetime as dt
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Request

from franklinwh_bridge.api.scheduler_api import _gateway_points
from franklinwh_bridge.gateway.energy_flow import integrate, node_totals
from franklinwh_bridge.gateway.scheduler_sensors import _ENERGY_SOURCES, snapshot
from franklinwh_bridge.store.metrics import query_metrics_daterange

router = APIRouter(prefix="/api/energy", tags=["energy"])

# Enough resolution to keep the merit order honest without pulling a day of raw
# samples into memory: ~2-minute buckets over 24h.
_TARGET_BUCKETS = 720
_MIN_BUCKET_S = 60
_MAX_SPAN_DAYS = 366


def _day_bounds(day: str | None) -> tuple[float, float, str]:
    """Resolve ``?day=YYYY-MM-DD`` to a local-midnight span.

    Local, not UTC: the aGate's own daily totals reset at gateway-local
    midnight, so a UTC day would disagree with every other figure on the page
    by the size of the timezone offset. See config/clock.py for why the
    container's zone is asserted at startup.
    """
    if day is None:
        today = dt.date.today()
    else:
        try:
            today = dt.date.fromisoformat(day)
        except ValueError:
            raise HTTPException(
            400, f"Invalid day '{day}' — expected YYYY-MM-DD"
        ) from None

    start = dt.datetime.combine(today, dt.time.min)
    end = start + dt.timedelta(days=1)
    return start.timestamp(), end.timestamp(), today.isoformat()


@router.get("/flow")
async def energy_flow(
    request: Request,
    day: str | None = None,
    start: float | None = None,
    end: float | None = None,
    gateway: str = "default",
):
    """Directional energy flows for a day (default today) or an explicit span.

    ``day`` is the common case and what the dashboards use. ``start``/``end``
    epoch seconds cover week/month/year views without a second endpoint.
    """
    db: aiosqlite.Connection = request.app.state.db

    if start is not None and end is not None:
        if end <= start:
            raise HTTPException(400, "end must be greater than start")
        if (end - start) > _MAX_SPAN_DAYS * 86400:
            raise HTTPException(400, f"Span cannot exceed {_MAX_SPAN_DAYS} days")
        span_start, span_end, label = start, end, "custom"
    else:
        span_start, span_end, label = _day_bounds(day)

    # Cap the end at now. Querying to a future midnight is not an error — it is
    # what "today" means before midnight — but the trailing empty buckets would
    # otherwise stretch the median step and loosen the outage cap.
    now = time.time()
    query_end = min(span_end, now)
    if query_end <= span_start:
        flows = integrate([])
    else:
        bucket = max(_MIN_BUCKET_S, int((query_end - span_start) / _TARGET_BUCKETS))
        rows = await query_metrics_daterange(
            db, span_start, query_end, bucket, gateway_id=gateway
        )
        flows = integrate(rows)

    nodes = node_totals(flows)
    span_s = query_end - span_start

    return {
        "day": label,
        "start": span_start,
        "end": span_end,
        "gateway_id": gateway,
        "flows": {k: v for k, v in flows.items()
                  if k not in ("residual_kwh", "covered_s", "samples")},
        "nodes": nodes,
        "quality": {
            # What fraction of the elapsed span actually had samples. Below ~1
            # the totals are an under-report, and the UI should say so rather
            # than present a short day as a quiet one.
            "coverage": round(flows["covered_s"] / span_s, 3) if span_s > 0 else 0.0,
            "covered_s": flows["covered_s"],
            "samples": flows["samples"],
            "residual_kwh": flows["residual_kwh"],
        },
    }


@router.get("/totals")
async def energy_totals_endpoint(request: Request, gateway: str = "default"):
    """Lifetime and period kWh, straight from the aGate's own counters.

    Measured, not reconstructed — these are the figures to trust when they
    disagree with the Sankey's arcs.

    Projected out of the same sensor snapshot the Automation Builder and the
    schedule engine read, rather than re-deriving the counters here. A second
    derivation is a second thing to drift: the whole point of these being the
    trustworthy numbers is that there is exactly one of them.
    """
    # Ask the registry, not the points dict. _gateway_points merges several
    # gateway-independent sources (constants, billing, demand), so it is never
    # empty — a typo'd gateway would otherwise return a full page of nulls and
    # read as "this gateway generated nothing" rather than "no such gateway".
    registry = getattr(request.app.state, "registry", None)
    if registry is None or registry.get(gateway) is None:
        raise HTTPException(404, f"Unknown gateway '{gateway}'")

    points = _gateway_points(request, gateway)
    if not points:
        raise HTTPException(503, f"No cached data for gateway '{gateway}' yet")

    snap = snapshot(points)

    def pick(suffix: str) -> dict[str, float | None]:
        return {
            src: snap.get(f"energy.{src}.{suffix}")
            for src in _ENERGY_SOURCES
        }

    return {
        "gateway_id": gateway,
        "lifetime_kwh": pick("total_kwh"),
        "today_kwh": pick("today_kwh"),
        "this_week_kwh": pick("this_week_kwh"),
        "this_month_kwh": pick("this_month_kwh"),
        "ytd_kwh": pick("ytd_kwh"),
    }
