"""Root-cause diagnostics for a gateway's Modbus connectivity.

Read-only: runs a battery of independent checks (TCP reachability to the
Modbus port and the aGate's Local API port, plus a live Modbus
protocol-level read reusing the poller's existing session) and cross-checks
them against what the poller itself reports, then synthesizes a plain-
English verdict.

This does NOT change the poller's error-handling behavior. It exists
because the poller's own ``connected``/``quality`` state can lag or miss
certain failure modes entirely (some read paths in the underlying
franklinwh-modbus library swallow exceptions and return ``{}``, and the
poller's own "extra point reads" path only logs a warning) — see
docs/vendor-issues.md and CHANGELOG for background. The diagnose tool's
job is to independently establish ground truth at the moment it's run.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from franklinwh_bridge.gateway.net_probe import tcp_probe

if TYPE_CHECKING:
    from franklinwh_bridge.gateway.instance import GatewayInstance

# aGate's second local channel (franklinwh-local), same IP as Modbus.
# Not yet a bridge dependency/config field — TCP reachability only for now.
LOCAL_API_PORT = 9000

# How far back to look in the shared log ring buffer for the specific
# "Extra point reads failed" warning that indicates a silently-broken
# Modbus session (see modbus/poller.py's _poll_once). The log buffer entries
# for this message carry no gateway_id (the log line has no "Gateway X:"
# prefix), so this signal is bridge-wide, not scoped to one gateway.
_RECENT_WARNING_WINDOW_S = 300
_EXTRA_READ_WARNING_SUBSTR = "Extra point reads failed"


async def run_diagnostics(inst: GatewayInstance, log_buffer: Any | None = None) -> dict:
    """Run all connectivity checks for one gateway and return a report dict."""
    if inst.config.mock:
        return {
            "gateway_id": inst.gateway_id,
            "timestamp": time.time(),
            "mock": True,
            "checks": {},
            "poller_state": None,
            "health": inst.status.health,
            "recent_extra_read_warnings": 0,
            "verdict": (
                "This is a mock gateway — synthetic data, no real Modbus "
                "connection to diagnose."
            ),
        }

    host = inst.config.host
    port = inst.config.port

    tcp_modbus = await tcp_probe(host, port)
    tcp_local_api = await tcp_probe(host, LOCAL_API_PORT)

    if inst.poller is not None and hasattr(inst.poller, "probe_protocol"):
        protocol = await inst.poller.probe_protocol()
        poller_state = _poller_state_dict(inst)
    else:
        protocol = {
            "ok": False,
            "latency_ms": None,
            "error": "Poller not running for this gateway",
            "error_type": "not_connected",
        }
        poller_state = None

    recent_warnings = _count_recent_extra_read_warnings(log_buffer)

    verdict = _build_verdict(
        tcp_modbus=tcp_modbus.ok,
        tcp_local_api=tcp_local_api.ok,
        protocol=protocol,
        poller_state=poller_state,
        recent_warnings=recent_warnings,
    )

    return {
        "gateway_id": inst.gateway_id,
        "timestamp": time.time(),
        "checks": {
            "tcp_502_modbus": tcp_modbus.to_dict(),
            "tcp_9000_local_api": tcp_local_api.to_dict(),
            "modbus_protocol": protocol,
        },
        "poller_state": poller_state,
        "health": inst.status.health,
        # Which register home load comes from, configured vs in use (#35):
        # the first thing to check when a user's load figures look wrong.
        "home_load_source": _home_load_source(inst),
        "recent_extra_read_warnings": recent_warnings,
        "verdict": verdict,
    }


def _home_load_source(inst: GatewayInstance) -> dict:
    """Configured vs in-use home load register (#35). Never fails diagnostics."""
    try:
        points = inst.latest_points() or {}
    except Exception:
        points = {}
    return {
        "configured": getattr(inst.config, "home_load_source", "standard"),
        "in_use": points.get("home_load_source") if isinstance(points, dict) else None,
    }


def _poller_state_dict(inst: GatewayInstance) -> dict:
    state = inst.poller.state
    now = time.time()
    return {
        "connected": state.connected,
        "consecutive_errors": state.consecutive_errors,
        "errors_total": state.errors_total,
        "polls_total": state.polls_total,
        "last_poll_ts": state.last_poll_ts,
        "seconds_since_last_poll": (
            round(now - state.last_poll_ts, 1) if state.last_poll_ts else None
        ),
        "last_error": state.last_error,
    }


def _count_recent_extra_read_warnings(log_buffer: Any | None) -> int:
    if log_buffer is None:
        return 0
    cutoff = time.time() - _RECENT_WARNING_WINDOW_S
    count = 0
    for entry in log_buffer:
        if entry.get("ts", 0) < cutoff:
            continue
        if _EXTRA_READ_WARNING_SUBSTR in entry.get("message", ""):
            count += 1
    return count


def _build_verdict(
    *,
    tcp_modbus: bool,
    tcp_local_api: bool,
    protocol: dict,
    poller_state: dict | None,
    recent_warnings: int,
) -> str:
    if not tcp_modbus and not tcp_local_api:
        return (
            "aGate appears unreachable on the network — neither the Modbus "
            "port (502) nor the Local API port (9000) can be opened. Check "
            "the aGate's power and network connection, and confirm the "
            "configured host/IP is still correct."
        )

    if not tcp_modbus and tcp_local_api:
        return (
            "aGate is up and on the network (Local API port 9000 is open), "
            "but the Modbus port (502) is not accepting connections. The "
            "aGate's Modbus server specifically may be down, disabled, or "
            "restarting — try power-cycling the aGate, or check its Modbus "
            "settings."
        )

    if not protocol.get("ok"):
        error_type = protocol.get("error_type")
        if error_type == "not_connected":
            return (
                "Modbus port (502) is reachable, but the bridge has no "
                "active Modbus session to probe — the poller hasn't "
                "connected yet. Check the poller/connection logs."
            )
        if error_type == "dead_session":
            return (
                "Modbus port (502) is open, but the bridge's existing "
                "Modbus session is dead (broken pipe / connection reset). "
                "The socket needs to be reconnected — this matches a known "
                "bridge-side gap where that doesn't happen automatically; "
                "restarting the bridge should recover it."
            )
        if error_type == "not_responding":
            return (
                "Modbus port (502) is open, but the aGate isn't responding "
                "to Modbus reads within the timeout. Its Modbus stack may "
                "be overloaded or hung."
            )
        return (
            "Modbus port (502) is open, but a protocol-level read failed: "
            f"{protocol.get('error')}"
        )

    # TCP and live protocol read both succeeded — cross-check against what
    # the poller itself believes, since the poller's own state can be stale.
    if poller_state and (not poller_state["connected"] or recent_warnings > 0):
        return (
            "aGate is reachable and responding to Modbus reads right now, "
            "but the bridge's own poller state disagrees (reports "
            "disconnected and/or has been logging repeated read failures "
            f"— {recent_warnings} in the last {_RECENT_WARNING_WINDOW_S}s). "
            "This looks like the bridge's internal connection state is "
            "wedged rather than an aGate problem — restarting the bridge "
            "should recover it."
        )

    return "Modbus connection is healthy: TCP and protocol-level reads both succeeded."
