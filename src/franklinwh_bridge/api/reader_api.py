"""Modbus reader in the SunSpec Explorer (#288): the franklinwh-modbus reader, in-process.

The owner (2026-10-19): use the library's reader "as a library for now, until
something better comes along". It's the library's ``tools/modbus_sunspec2_reader.py``,
vendored unchanged (``franklinwh_bridge/vendor``), and it only reads.

One Modbus client per aGate: the reader opens its own connection, so the bridge
takes the gateway's Modbus lock, disconnects its own controller, runs the reader,
and releases the lock; the poller reconnects on its next poll. The aGate serves
SunSpec at PDU address 0, so the scan base is 0, not the reader's default 40000.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import io
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reader"])

#: Registers in one raw read (one Modbus request carries at most 125).
MAX_RAW_COUNT = 125
#: A full model scan with match takes a few seconds; never hold the lock longer.
RUN_TIMEOUT_S = 90
#: One reader run at a time, across gateways: it pauses polling while it runs.
_run_lock = asyncio.Lock()


class ReaderRequest(BaseModel):
    gateway_id: str = "default"
    raw: str | None = Field(None, pattern=r"^\d{1,5}(:\d{1,3})?$",
                            description="start:count, e.g. 15500:14 (PDU addresses)")
    models: str | None = Field(None, pattern=r"^[\d,\-\s]+$", description="e.g. 1,701-704")
    point: str | None = Field(None, pattern=r"^\d+\.\w+$", description="e.g. 714.DCW")
    detail: Literal["minimal", "basic", "values", "detailed", "full"] = "basic"
    nz: bool = False
    match: bool = False


class ReaderResult(BaseModel):
    output: str
    error: str | None = None
    elapsed_s: float


def _options(body: ReaderRequest) -> dict[str, Any]:
    """The reader CLI's own rules (its main()), applied to the request."""
    from franklinwh_bridge.vendor import modbus_sunspec2_reader as rdr

    raw = body.raw
    if raw:
        start, _, count = raw.partition(":")
        n = int(count or 1)
        if not 1 <= n <= MAX_RAW_COUNT:
            raise HTTPException(400, f"count must be 1-{MAX_RAW_COUNT}")
        if int(start) + n > 65536:
            raise HTTPException(400, "range runs past register 65535")
    detail = body.detail
    if body.match and detail in ("minimal", "basic"):
        detail = "values"  # match needs point values, as the CLI forces
    return {
        "models_to_scan": rdr.parse_model_spec(body.models) if body.models else None,
        "detail_level": detail,
        "specific_point": body.point,
        "raw_read_spec": raw,
        # Raw only, no match: skip the model scan, as the CLI does.
        "scan_models": not (raw and not body.models and not body.match),
    }


def _run_reader(host: str, port: int, unit: int, timeout: float,
                opts: dict[str, Any], nz: bool, match: bool) -> tuple[str, str | None]:
    from franklinwh_bridge.vendor import modbus_sunspec2_reader as rdr

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            info, err = rdr.read_sunspec_device(
                host, port=port, unit=unit, timeout=timeout, base_address=0, **opts,
            )
            if info:
                rdr.print_device_info(info, opts["detail_level"], show_nonzero=nz, do_match=match)
    finally:
        # The reader's close() is pysunspec2's no-op for TCP devices (#64), so
        # its socket only closes when the device is collected. Collect now, so
        # the aGate's one session is free again before the poller reconnects.
        info = None
        gc.collect()
    return buf.getvalue(), err


@router.post("/reader/run", response_model=ReaderResult,
             dependencies=[Depends(require_role("admin"))])
async def run_reader(body: ReaderRequest, request: Request) -> ReaderResult:
    """Run the franklinwh-modbus reader against a gateway. Admin, read-only."""
    registry = getattr(request.app.state, "registry", None)
    inst = registry.get(body.gateway_id) if registry else None
    if inst is None:
        raise HTTPException(404, f"gateway '{body.gateway_id}' is not running")
    if inst.config.mock:
        raise HTTPException(409, "the reader needs a real aGate; this is a demo gateway")
    if _run_lock.locked():
        raise HTTPException(409, "the reader is already running")
    opts = _options(body)
    cfg = inst.config
    t0 = time.monotonic()
    async with _run_lock, inst.modbus_lock:
        # Hand the aGate's one Modbus connection to the reader for this run;
        # the poller reconnects under the same lock afterwards.
        if inst.controller is not None:
            await asyncio.to_thread(inst.controller.disconnect)
        # The poller's own vendor-register connection (pymodbus, #243) too, or
        # the reader would be a third client.
        close_vreg = getattr(getattr(inst, "poller", None), "_close_vreg_client", None)
        if close_vreg is not None:
            await asyncio.to_thread(close_vreg)
        logger.info("Reader run on gateway %s: raw=%s models=%s point=%s nz=%s match=%s",
                    body.gateway_id, body.raw, body.models, body.point, body.nz, body.match)
        try:
            output, err = await asyncio.wait_for(
                asyncio.to_thread(_run_reader, cfg.host, cfg.port, cfg.unit_id,
                                  float(cfg.timeout or 10), opts, body.nz, body.match),
                RUN_TIMEOUT_S,
            )
        except TimeoutError:
            output, err = "", f"no result within {RUN_TIMEOUT_S} s"
        finally:
            # Give the connection back before releasing the lock. The poller
            # doesn't notice a dropped controller (the library returns empty
            # reads, not errors), so it would poll a dead connection.
            if inst.controller is not None:
                try:
                    await asyncio.to_thread(inst.controller.connect)
                except Exception as exc:  # the poller's own reconnect takes over
                    logger.warning("Reader: reconnect after the run failed: %s", exc)
    return ReaderResult(output=output, error=err, elapsed_s=round(time.monotonic() - t0, 2))
