"""Diagnostics over REST (#342): the Modbus write audit trail, as JSON or CSV."""

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
