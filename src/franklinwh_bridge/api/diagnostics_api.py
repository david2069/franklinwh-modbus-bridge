"""Diagnostics over REST: the Modbus write audit trail (#342) and the app log export (#320)."""

from __future__ import annotations

import csv
import io
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from franklinwh_bridge import __version__
from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.store import modbus_audit
from franklinwh_bridge.store.db import get_gateways, query_logs
from franklinwh_bridge.support import log_export

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"],
                   dependencies=[Depends(require_role("admin"))])

#: Export schema version (#342 §4): bump when a column changes.
SCHEMA_VERSION = 1


@router.get("/modbus-writes")
async def modbus_writes(
    request: Request,
    since: float | None = Query(None, description="epoch seconds"),
    until: float | None = Query(None, description="epoch seconds"),
    gateway: str | None = None,
    limit: int = Query(1000, ge=1, le=100_000),
    format: Literal["json", "csv"] = "json",  # noqa: A002
):
    """Every SunSpec write: time, register, raw values, the aGate's reply, who asked.

    No serial or IP is in these rows, so nothing needs masking.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "the database is not ready")
    await modbus_audit.flush(db)  # include writes still queued
    rows = await modbus_audit.query(db, since=since, until=until, gateway_id=gateway,
                                    limit=limit)
    if format == "json":
        return {"schema_version": SCHEMA_VERSION, "bridge_version": __version__,
                "count": len(rows), "writes": rows}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "gateway_id", "initiator", "address", "values", "result", "detail"])
    for r in rows:
        w.writerow([f"{r['ts']:.3f}", r["gateway_id"], r["initiator"], r["address"],
                    json.dumps(r["values"]), r["result"], r["detail"] or ""])
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=modbus_writes.csv",
        "X-Schema-Version": str(SCHEMA_VERSION)})


@router.get("/logs")
async def export_logs(
    request: Request,
    start_ts: float | None = Query(None, description="epoch seconds"),
    end_ts: float | None = Query(None, description="epoch seconds"),
    level: str | None = None,
    gateway: str | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: int = Query(log_export.MAX_LINES, ge=1, le=log_export.MAX_LINES),
    format: Literal["text", "json"] = "text",  # noqa: A002
):
    """The app log for the Logs tab's filter, as a file; serials and IPs masked (#320).

    Same filters as ``GET /api/logs``. Oldest first; past ``limit`` the newest are kept.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "the database is not ready")
    result = await query_logs(db, start_ts=start_ts, end_ts=end_ts, level=level,
                              gateway_id=gateway, source=source, search=search,
                              limit=limit, max_limit=log_export.MAX_LINES)
    known = log_export.known_identifiers(await get_gateways(db),
                                         getattr(request.app.state, "registry", None))
    filters = {"start_ts": start_ts, "end_ts": end_ts, "level": level,
               "gateway": gateway, "source": source, "search": search}
    export = log_export.build(list(reversed(result["logs"])), known, filters=filters,
                              version=__version__, total=result["total"])
    if format == "json":
        content, media_type = log_export.render_json(export), "application/json"
    else:
        content, media_type = log_export.render_text(export), "text/plain; charset=utf-8"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename={log_export.filename(format)}",
        "X-Schema-Version": str(log_export.SCHEMA_VERSION)})
