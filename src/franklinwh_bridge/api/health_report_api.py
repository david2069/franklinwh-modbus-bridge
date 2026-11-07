"""Scheduled health check (#296 item 51): the daily report, the weekly battery report
and the daily snapshot.
The work is in ``health_report.py``.

Reads need a signed-in user; running a report now and the switch need admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from franklinwh_bridge import health_report
from franklinwh_bridge.api.auth import require_auth, require_role

router = APIRouter(prefix="/api/health", tags=["health"])
_ADMIN = require_role("admin")


_KIND = r"^(daily|weekly)$"


class SettingsUpdate(BaseModel):
    daily: bool | None = None
    weekly: bool | None = None


def _reporter(request: Request) -> health_report.HealthReporter:
    rep = getattr(request.app.state, "health_reporter", None)
    if rep is None:
        raise HTTPException(503, "The health check is not running yet")
    return rep


@router.get("/report", dependencies=[Depends(require_auth)])
async def latest(request: Request, kind: str = Query("daily", pattern=_KIND)) -> dict:
    """The latest report: ``{id, kind, period, ts, level, headline, report}``, or
    ``{report: null}`` before the first one."""
    return await health_report.latest_report(request.app.state.db, kind) or {"report": None}


@router.post("/report", dependencies=[Depends(_ADMIN)])
async def run_now(request: Request, kind: str = Query("daily", pattern=_KIND)) -> dict:
    """Build and store a report now: today's daily one, or the weekly one for last week.
    Sends nothing."""
    return await _reporter(request).run(notify=False, kind=kind)


@router.get("/reports", dependencies=[Depends(require_auth)])
async def reports(request: Request, kind: str | None = Query(None, pattern=_KIND),
                  limit: int = Query(60, ge=1, le=400)) -> dict:
    """Past reports, newest first, without their bodies."""
    return {"reports": await health_report.list_reports(request.app.state.db, kind, limit)}


@router.get("/reports/{report_id}", dependencies=[Depends(require_auth)])
async def report(request: Request, report_id: int) -> dict:
    r = await health_report.get_report(request.app.state.db, report_id)
    if r is None:
        raise HTTPException(404, "No such report")
    return r


@router.get("/snapshots", dependencies=[Depends(require_auth)])
async def snapshots(request: Request, gateway: str | None = None,
                    days: int = Query(31, ge=1, le=3660)) -> dict:
    """Daily snapshot rows, newest first. ``gateway=`` (an ID; empty = the bridge's row)."""
    return {"snapshots": await health_report.snapshots(request.app.state.db, gateway, days)}


@router.get("/settings", dependencies=[Depends(require_auth)])
async def get_settings(request: Request) -> dict:
    db = request.app.state.db
    return {"daily": await health_report.daily_on(db),
            "daily_at": "{:02d}:{:02d}".format(*health_report.DAILY_AT),
            "weekly": await health_report.weekly_on(db),
            "weekly_at": "Monday {:02d}:{:02d}".format(*health_report.WEEKLY_AT[1:])}


@router.put("/settings", dependencies=[Depends(_ADMIN)])
async def put_settings(request: Request, body: SettingsUpdate) -> dict:
    if body.daily is not None:
        await health_report.set_daily(request.app.state.db, body.daily)
    if body.weekly is not None:
        await health_report.set_weekly(request.app.state.db, body.weekly)
    return await get_settings(request)
