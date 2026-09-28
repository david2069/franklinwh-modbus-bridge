"""Point-history REST endpoints — the reader for what the poller has stored.

:mod:`store.point_history` has been writing ``metric_samples`` and purging it on
retention since it was wired into the poll loop, but nothing could read it back:
the AC and DC charts were live-only, so "chart yesterday afternoon" had no route
to ask. These are that route.

Three concerns, deliberately separate:

* ``/config`` — what is recorded, how often, for how long. Writing it is a
  control operation: turning history on commits disk on the user's machine.
* ``/series`` — the data itself, bucketed to something drawable.
* ``/storage`` — what it is costing now, and what a candidate config would cost,
  so the trade-off is visible before it is chosen rather than discovered later.
"""

from __future__ import annotations

import datetime as dt
import time

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from franklinwh_bridge.api.auth import require_capability
from franklinwh_bridge.store import point_history

router = APIRouter(prefix="/api/point-history", tags=["point-history"])

# Hoisted: Depends() in an argument default is evaluated once at import, and
# building it inline trips ruff B008.
_REQUIRE_CONTROL = Depends(require_capability("control"))

#: A chart is drawn on a screen, not a spreadsheet. Bucketing to roughly this
#: many points keeps a month-long request answerable without shipping 250k
#: samples per series to a canvas that can show ~1500 of them.
_TARGET_BUCKETS = 900
_MIN_BUCKET_S = 1
_MAX_SPAN_DAYS = 400


class ConfigPatch(BaseModel):
    """Partial update. Absent fields keep their current value.

    Bounds are enforced in :func:`point_history.get_config` as well — these are
    here so a bad request is a 422 with a field name rather than a silent clamp.
    """

    enabled: bool | None = None
    points: list[str] | None = None
    interval_s: int | None = Field(default=None, ge=5, le=3600)
    retention_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("points")
    @classmethod
    def _known_points(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        unknown = [p for p in v if p not in point_history.HISTORISED_POINTS]
        if unknown:
            raise ValueError(f"unknown point(s): {', '.join(sorted(unknown))}")
        return v


def _resolve_span(
    day: str | None, start: float | None, end: float | None
) -> tuple[float, float]:
    """Resolve ``?day=`` or ``?start=&end=`` to an epoch span.

    ``day`` uses local midnight for the same reason the energy endpoints do: the
    aGate's daily counters roll at gateway-local midnight, so a UTC day would
    disagree with every other figure on the page by the offset.
    """
    if day is not None:
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            raise HTTPException(
                400, f"Invalid day '{day}' — expected YYYY-MM-DD"
            ) from None
        begin = dt.datetime.combine(d, dt.time.min)
        return begin.timestamp(), (begin + dt.timedelta(days=1)).timestamp()

    if start is None or end is None:
        # Default to the last six hours — the span the live charts top out at,
        # so the static view opens on something comparable.
        now = time.time()
        return now - 6 * 3600, now

    if end <= start:
        raise HTTPException(400, "end must be after start")
    if (end - start) > _MAX_SPAN_DAYS * 86400:
        raise HTTPException(400, f"span exceeds {_MAX_SPAN_DAYS} days")
    return start, end


@router.get("/config")
async def get_config(request: Request):
    """Current recording settings, plus the catalogue a client can offer."""
    db: aiosqlite.Connection = request.app.state.db
    cfg = await point_history.get_config(db)
    return {
        "config": cfg,
        "available_points": list(point_history.HISTORISED_POINTS),
        "projected_bytes": point_history.project_bytes(
            len(cfg["points"]), cfg["interval_s"], cfg["retention_days"]
        ),
    }


@router.put("/config")
async def put_config(
    request: Request, patch: ConfigPatch, _user: dict = _REQUIRE_CONTROL
):
    """Update recording settings.

    Takes effect on the next poll tick — the loop re-reads this each pass rather
    than caching it, so there is nothing to restart. Shrinking retention does
    not delete anything here; the purge pass applies it on its own schedule.
    """
    db: aiosqlite.Connection = request.app.state.db
    updates = patch.model_dump(exclude_none=True)
    cfg = (
        await point_history.set_config(db, updates)
        if updates
        else await point_history.get_config(db)
    )
    return {
        "config": cfg,
        "projected_bytes": point_history.project_bytes(
            len(cfg["points"]), cfg["interval_s"], cfg["retention_days"]
        ),
    }


@router.get("/series")
async def get_series(
    request: Request,
    points: str,
    day: str | None = None,
    start: float | None = None,
    end: float | None = None,
    bucket_s: float | None = None,
    gateway: str = "default",
):
    """Historical series for a comma-separated ``points`` list.

    ``bucket_s`` is chosen automatically from the span unless given. The
    response echoes the resolved span and bucket so a caller can label the
    axis with what it actually got rather than what it asked for.
    """
    db: aiosqlite.Connection = request.app.state.db

    wanted = [p.strip() for p in points.split(",") if p.strip()]
    if not wanted:
        raise HTTPException(400, "points must name at least one point")
    unknown = [p for p in wanted if p not in point_history.HISTORISED_POINTS]
    if unknown:
        raise HTTPException(400, f"unknown point(s): {', '.join(sorted(unknown))}")

    start_ts, end_ts = _resolve_span(day, start, end)

    if bucket_s is None:
        bucket_s = max(_MIN_BUCKET_S, (end_ts - start_ts) / _TARGET_BUCKETS)
    elif bucket_s <= 0:
        # Explicit 0 means "don't bucket", which is honest for a short span and
        # is how the caller gets raw samples.
        bucket_s = None

    series = await point_history.query_points(
        db, wanted, start_ts, end_ts, bucket_seconds=bucket_s, gateway_id=gateway
    )

    cfg = await point_history.get_config(db)
    return {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "bucket_s": bucket_s,
        "gateway_id": gateway,
        # An empty chart has two very different causes, and the user should not
        # have to guess which: nothing was recorded for this span, or recording
        # has never been switched on at all.
        "recording_enabled": cfg["enabled"],
        "series": series,
        "counts": {pid: len(rows) for pid, rows in series.items()},
    }


@router.get("/storage")
async def get_storage(
    request: Request,
    points: int | None = None,
    interval_s: float | None = None,
    retention_days: float | None = None,
):
    """What point history costs now, and what a candidate config would cost.

    The projection query params let Settings show the consequence of a change
    while the user is still deciding, without saving it first.
    """
    db: aiosqlite.Connection = request.app.state.db
    cfg = await point_history.get_config(db)
    stats = await point_history.storage_stats(db)

    candidate = point_history.project_bytes(
        len(cfg["points"]) if points is None else points,
        cfg["interval_s"] if interval_s is None else interval_s,
        cfg["retention_days"] if retention_days is None else retention_days,
    )
    return {
        "actual": stats | {"bytes": stats["rows"] * point_history.BYTES_PER_ROW},
        "projected_bytes": candidate,
        "bytes_per_row": point_history.BYTES_PER_ROW,
        "config": cfg,
    }
