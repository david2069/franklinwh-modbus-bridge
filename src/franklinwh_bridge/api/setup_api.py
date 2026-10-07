"""First-run setup wizard — REST API (docs/setup-wizard-design.md §6).

The wizard asks what the user wants (connect a real aGate, or explore with a
demo gateway), finds the aGate on the LAN, and finishes with a Home Assistant
checklist. Every step is an endpoint here, so the CLI can drive the same flow.

Discovery goes through ``franklinwh_modbus.discovery`` — the library owns all
Modbus I/O; the bridge never reads a register itself. It is read-only, and a
host that a running gateway already polls is never probed: it is reported as
"already connected" instead, so the wizard can't disturb a working poller.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from franklinwh_modbus import discovery
from pydantic import BaseModel, ConfigDict, Field

from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.api.gateways_api import (
    GatewayCreate,
    GatewayUpdate,
    add_gateway,
    patch_gateway,
)
from franklinwh_bridge.config.environment import (
    SCAN_PREFIX,
    candidate_subnets,
    detect_environment,
)
from franklinwh_bridge.config.supervisor import LOCAL_HA_ID
from franklinwh_bridge.store.db import (
    gateway_is_unconfigured,
    get_app_config,
    get_gateway,
    get_gateways,
    set_app_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

_REQUIRE_ADMIN = require_role("admin")

SETUP_STATE_KEY = "setup_state"
SetupState = Literal["pending", "done_real", "done_demo", "skipped"]

#: Tailscale's CGNAT range is not "private", but an aGate reached over a tailnet
#: lives there — scanned only when the user types it.
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")

#: Finished scans kept for polling; older ones are dropped.
_MAX_SCANS = 5

#: Discovery tuning. One attempt per host in a sweep (fast); two for a single
#: typed address, where the user is waiting on exactly that answer.
_SCAN_PROBE_TIMEOUT_S = 2.0
_PROBE_ATTEMPTS = 2


# ── state ─────────────────────────────────────────────────────────────────────

async def setup_status(db: aiosqlite.Connection) -> dict[str, Any]:
    """The wizard's state, as the UI and CLI see it.

    ``state`` is the stored value, except that ``pending`` reads as
    ``done_real`` once a real gateway has an address: an install configured
    through MODBUS_HOST (or by hand in Settings) is already set up.
    """
    stored = await get_app_config(db, SETUP_STATE_KEY, "pending") or "pending"
    gateways = await get_gateways(db)
    has_real = any(not g.get("mock") and not gateway_is_unconfigured(g) for g in gateways)
    has_mock = any(g.get("mock") for g in gateways)
    state = "done_real" if stored == "pending" and has_real else stored
    return {
        "state": state,
        "has_real_gateway": has_real,
        "has_mock": has_mock,
        "environment": detect_environment(),
    }


class SetupStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: SetupState


@router.get("/state")
async def get_setup_state(request: Request):
    """Whether the wizard should open. Any signed-in user may ask; only admins
    are shown the wizard (the UI checks the role)."""
    return await setup_status(request.app.state.db)


@router.post("/state", dependencies=[Depends(_REQUIRE_ADMIN)])
async def post_setup_state(body: SetupStateUpdate, request: Request):
    """Record a choice — typically ``skipped``, or ``pending`` to run it again."""
    db = request.app.state.db
    await set_app_config(db, SETUP_STATE_KEY, body.state)
    return await setup_status(db)


# ── subnets ───────────────────────────────────────────────────────────────────

@router.get("/subnets", dependencies=[Depends(_REQUIRE_ADMIN)])
async def get_subnets(request: Request):
    """Subnets to search, each with where it came from (see environment.py)."""
    return {
        "environment": detect_environment(),
        "subnets": await candidate_subnets(request.headers.get("host")),
    }


def normalise_subnet(raw: str) -> tuple[str, bool]:
    """Validate a subnet for scanning; return ``(subnet, allow_public)``.

    IPv4 only. Wider than /24 is narrowed to the /24 that contains the given
    address (``192.168.0.0/16`` → ``192.168.0.0/24``). Private ranges only,
    plus Tailscale's 100.64.0.0/10.
    """
    try:
        net = ipaddress.ip_network(raw.strip(), strict=False)
    except ValueError as exc:
        raise HTTPException(400, f"'{raw}' is not a subnet (e.g. 192.168.1.0/24)") from exc
    if net.version != 4:
        raise HTTPException(400, "Only IPv4 subnets can be searched")
    if net.prefixlen < SCAN_PREFIX:
        addr = ipaddress.ip_interface(raw.strip()).ip if "/" in raw else net.network_address
        net = ipaddress.ip_network(f"{addr}/{SCAN_PREFIX}", strict=False)
    tailscale = net.subnet_of(_TAILSCALE)
    if not net.is_private and not tailscale:
        raise HTTPException(400, f"{net} is not a private network address range")
    return str(net), tailscale


# ── result rows ───────────────────────────────────────────────────────────────

def _device_type(model: str | None) -> str:
    """'mac1' for a Meter Adaptor Collar, else 'agate' — from the model string."""
    m = (model or "").upper().replace("-", "").replace(" ", "")
    return "mac1" if "MAC1" in m or "METERADAPTOR" in m else "agate"


def result_row(res: discovery.DiscoveryResult) -> dict[str, Any]:
    """A discovery result as the wizard shows it."""
    row = res.to_dict()
    if res.is_franklinwh:
        row["kind"] = "agate"
        dtype = _device_type(res.model)
        row["device_type"] = dtype
        tail = (res.serial or "")[-4:]
        label = "MAC-1" if dtype == "mac1" else "aGate"
        row["suggested_name"] = f"{label} {tail}".strip()
    elif res.status == discovery.SUNSPEC:
        row["kind"] = "sunspec_other"
    else:
        row["kind"] = res.status  # unknown | closed
    return row


def _configured_row(host: str, port: int, gw: dict) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "kind": "configured",
        "gateway_id": gw["id"],
        "name": gw.get("name"),
        "summary": f"{gw.get('name') or gw['id']} — already set up in this bridge",
    }


def _host_key(row: dict) -> tuple:
    try:
        return (0, int(ipaddress.ip_address(row["host"])))
    except ValueError:
        return (1, row["host"])


def _resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


async def _polled_hosts(request: Request) -> dict[tuple[str, int], dict]:
    """``(ip, port) -> gateway`` for every real gateway with a running instance.

    These are never probed: the probe would compete with the gateway's own
    Modbus connection.
    """
    registry = getattr(request.app.state, "registry", None)
    out: dict[tuple[str, int], dict] = {}
    if registry is None:
        return out
    for gw in await get_gateways(request.app.state.db):
        if gw.get("mock") or gateway_is_unconfigured(gw) or registry.get(gw["id"]) is None:
            continue
        ip = await asyncio.to_thread(_resolve, str(gw["host"]))
        out[(ip, int(gw.get("port") or 502))] = gw
    return out


# ── scan ──────────────────────────────────────────────────────────────────────

@dataclass
class _Scan:
    id: str
    subnets: list[str]
    port: int
    hosts_total: int
    hosts_done: int = 0
    status: str = "running"  # running | done | error
    results: list[dict] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "scan_id": self.id,
                "subnets": self.subnets,
                "status": self.status,
                "hosts_total": self.hosts_total,
                "hosts_done": self.hosts_done,
                "results": sorted(self.results, key=_host_key),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


def _run_scan(scan: _Scan, plans: list[tuple[str, bool]], skip: dict, unit_id: int) -> None:
    """Blocking: sweep each subnet through the library. Runs in a thread."""

    def check(host: str, port: int, timeout: float) -> bool:
        with scan.lock:
            scan.hosts_done += 1
        if (host, port) in skip:
            with scan.lock:
                scan.results.append(_configured_row(host, port, skip[(host, port)]))
            return False
        return discovery.port_open(host, port, timeout)

    def found(res: discovery.DiscoveryResult) -> None:
        with scan.lock:
            scan.results.append(result_row(res))

    try:
        for subnet, allow_public in plans:
            discovery.scan(
                subnet,
                port=scan.port,
                unit_id=unit_id,
                probe_timeout=_SCAN_PROBE_TIMEOUT_S,
                allow_public=allow_public,
                on_result=found,
                port_check=check,
            )
        status, error = "done", None
    except Exception as exc:  # report, never crash the bridge
        logger.warning("Setup scan %s failed: %s", scan.id, exc)
        status, error = "error", str(exc) or type(exc).__name__
    with scan.lock:
        scan.status, scan.error, scan.finished_at = status, error, time.time()


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subnets: list[str] = Field(..., min_length=1, max_length=4)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)


def _scans(request: Request) -> dict[str, _Scan]:
    scans = getattr(request.app.state, "setup_scans", None)
    if scans is None:
        scans = request.app.state.setup_scans = {}
    return scans


@router.post("/scan", status_code=202, dependencies=[Depends(_REQUIRE_ADMIN)])
async def start_scan(body: ScanRequest, request: Request):
    """Search subnets for aGates in the background; poll ``GET /scan/{id}``."""
    plans = [normalise_subnet(s) for s in body.subnets]
    plans = list(dict.fromkeys(plans))  # de-duplicate, keep order
    total = sum(ipaddress.ip_network(s).num_addresses - 2 for s, _ in plans)
    scan = _Scan(
        id=uuid.uuid4().hex[:12],
        subnets=[s for s, _ in plans],
        port=body.port,
        hosts_total=total,
    )
    scans = _scans(request)
    scans[scan.id] = scan
    for old in sorted(scans.values(), key=lambda s: s.started_at)[:-_MAX_SCANS]:
        scans.pop(old.id, None)

    skip = await _polled_hosts(request)
    task = asyncio.create_task(asyncio.to_thread(_run_scan, scan, plans, skip, body.unit_id))
    # Keep a reference so the task isn't garbage-collected mid-run.
    request.app.state.setup_scan_task = task
    logger.info("Setup scan %s started: %s", scan.id, ", ".join(scan.subnets))
    return scan.to_dict()


@router.get("/scan/{scan_id}", dependencies=[Depends(_REQUIRE_ADMIN)])
async def get_scan(scan_id: str, request: Request):
    scan = _scans(request).get(scan_id)
    if scan is None:
        raise HTTPException(404, "Scan not found (finished scans are kept briefly)")
    return scan.to_dict()


# ── probe one address ─────────────────────────────────────────────────────────

class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)


@router.post("/probe", dependencies=[Depends(_REQUIRE_ADMIN)])
async def probe_host(body: ProbeRequest, request: Request):
    """Identify one address (the wizard's *Enter address → Test*)."""
    host = body.host.strip()
    ip = await asyncio.to_thread(_resolve, host)
    gw = (await _polled_hosts(request)).get((ip, body.port))
    if gw is not None:
        return _configured_row(host, body.port, gw)
    res = await asyncio.to_thread(
        discovery.probe, host, body.port, body.unit_id,
        _SCAN_PROBE_TIMEOUT_S, _PROBE_ATTEMPTS,
    )
    return result_row(res)


# ── connect / demo ────────────────────────────────────────────────────────────

def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return s or fallback


async def _unique_id(db: aiosqlite.Connection, base: str) -> str:
    candidate, n = base, 2
    while await get_gateway(db, candidate) is not None:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)
    name: str = Field(..., min_length=1, max_length=120)
    device_type: str = Field(default="agate", pattern=r"^(agate|mac1)$")


@router.post("/connect", dependencies=[Depends(_REQUIRE_ADMIN)])
async def connect_gateway(body: ConnectRequest, request: Request):
    """Set up a real gateway at this address and start it.

    The first one fills the primary gateway (internally still id ``default``,
    which owns the unprefixed MQTT topics existing HA setups rely on); any
    further ones are added alongside.
    """
    db = request.app.state.db
    host = body.host.strip()
    for gw in await get_gateways(db):
        if (
            not gw.get("mock")
            and str(gw.get("host") or "").strip() == host
            and int(gw.get("port") or 502) == body.port
            and int(gw.get("unit_id") or 1) == body.unit_id
        ):
            raise HTTPException(
                409, f"'{gw.get('name') or gw['id']}' is already set up for {host}:{body.port}"
            )

    primary = await get_gateway(db, "default")
    if primary is not None and gateway_is_unconfigured(primary):
        gw_id = "default"
        await patch_gateway(
            gw_id,
            GatewayUpdate(
                host=host,
                port=body.port,
                unit_id=body.unit_id,
                name=body.name,
                device_type=body.device_type,
                description="",
            ),
            request,
        )
    else:
        gw_id = await _unique_id(db, _slug(body.name, "agate"))
        await add_gateway(
            GatewayCreate(
                gateway_id=gw_id,
                name=body.name,
                host=host,
                port=body.port,
                unit_id=body.unit_id,
                device_type=body.device_type,
            ),
            request,
        )

    await set_app_config(db, SETUP_STATE_KEY, "done_real")
    logger.info("Setup: gateway %s configured at %s:%d", gw_id, host, body.port)
    return {"gateway_id": gw_id, "gateway": await get_gateway(db, gw_id)}


class DemoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Demo aGate", min_length=1, max_length=120)
    # 0 single phase, 1 split phase (US), 2 three phase.
    ac_type: int = Field(default=0, ge=0, le=2)


@router.post("/demo", dependencies=[Depends(_REQUIRE_ADMIN)])
async def create_demo(body: DemoRequest, request: Request):
    """Create a demo (mock) gateway.

    Always a separate gateway — never the primary: the primary owns the
    unprefixed MQTT topics, and synthetic data there would land in the same HA
    entities (and long-term statistics) a real aGate uses later.
    """
    db = request.app.state.db
    gw_id = await _unique_id(db, "demo")
    await add_gateway(
        GatewayCreate(
            gateway_id=gw_id,
            name=body.name,
            mock=True,
            description="Demo gateway — simulated data and controls",
        ),
        request,
    )
    if body.ac_type:
        await patch_gateway(gw_id, GatewayUpdate(ac_type=body.ac_type), request)

    status = await setup_status(db)
    if status["state"] != "done_real":
        await set_app_config(db, SETUP_STATE_KEY, "done_demo")
    logger.info("Setup: demo gateway %s created", gw_id)
    return {"gateway_id": gw_id, "gateway": await get_gateway(db, gw_id)}


# ── Home Assistant checklist ──────────────────────────────────────────────────

def _ha_instance(request: Request):
    """The HA instance to check: the add-on's own, else the first connected one."""
    reg = getattr(request.app.state, "ha_registry", None)
    if reg is None:
        return None
    local = reg.get(LOCAL_HA_ID)
    if local is not None:
        return local
    for row in reg.status():
        if row.get("connected"):
            return reg.get(row["id"])
    return None


@router.get("/checklist", dependencies=[Depends(_REQUIRE_ADMIN)])
async def get_checklist(request: Request):
    """Step 6: is everything between the bridge and Home Assistant working?

    Each item is ``ok`` / ``fail`` / ``unknown`` (couldn't tell — shown as a
    manual step), with a ``detail`` for the UI.
    """
    env = detect_environment()
    items: list[dict[str, Any]] = []

    broker = getattr(request.app.state, "mqtt_broker", None) or {}
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    connected = bool(publisher is not None and publisher.state.connected)
    items.append({
        "id": "mqtt_broker",
        "status": "ok" if connected else "fail",
        "detail": (
            "Connected" if connected
            else "Waiting for an MQTT broker" if broker.get("waiting_for_broker")
            else "Not connected"
        ),
    })

    inst = _ha_instance(request)
    items.append({
        "id": "ha_connection",
        "status": "ok" if inst is not None and inst.connected else "fail",
        "detail": (
            f"{inst.name}" if inst is not None and inst.connected
            else "No Home Assistant connected"
        ),
    })

    integration: dict[str, Any] = {"id": "ha_mqtt_integration", "status": "unknown",
                                   "detail": "Check in Home Assistant"}
    entities: dict[str, Any] = {"id": "ha_entities", "status": "unknown",
                                "detail": "Check in Home Assistant", "count": None}
    if inst is not None and inst.connected:
        try:
            entries = await inst.ws_call({"type": "config_entries/get", "domain": "mqtt"})
            loaded = [e for e in entries or [] if e.get("state") == "loaded"]
            integration.update(
                status="ok" if loaded else "fail",
                detail="Set up" if loaded else "Not set up — confirm it under Discovered",
            )
        except Exception as exc:  # the token may not be allowed to ask
            logger.debug("MQTT config entry check failed: %s", exc)
        try:
            reg = await inst.ws_call({"type": "config/entity_registry/list"})
            n = sum(
                1 for e in reg or []
                if e.get("platform") == "mqtt"
                and str(e.get("unique_id") or "").startswith("franklinwh_")
            )
            entities.update(
                status="ok" if n else "fail",
                count=n,
                detail=f"{n} entities" if n else "None yet",
            )
        except Exception as exc:
            logger.debug("Entity registry check failed: %s", exc)
    items.extend([integration, entities])
    return {"environment": env, "items": items}
