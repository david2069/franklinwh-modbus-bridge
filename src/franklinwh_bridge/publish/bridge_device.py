"""The bridge's own HA device: "Bridge problem" and "Bridge heartbeat" (#324 v1.2).

One device for the bridge itself, beside the per-gateway devices, so a
problem shows even for a gateway that never answered (and so has no device).
Owner exception to the D1 identity freeze, for this device and these two
sensors only (2026-10-21). Shapes agreed with the DC bridge (its #286):

- **Bridge problem:** binary_sensor, device_class problem, diagnostic. State
  ``<ns>/bridge/problem`` (retained), JSON ``{problem, level, open, alerts[]}``;
  the attributes are the same JSON. **No availability topic**, so it stays
  readable while a gateway, or the bridge, is down.
- **Bridge heartbeat:** sensor, device_class timestamp, diagnostic. State
  ``<ns>/bridge/heartbeat`` (retained, ISO 8601 UTC) every 60 s from the alert
  monitor, so a stopped or hung bridge shows an old time. A documented HA
  automation alerts when it is 5 minutes old.

Both configs carry our ``sw_version`` marker, so the namespace change and
Unpublish remove them like the gateways' (install-lifecycle E6, E10).
"""

from __future__ import annotations

from typing import Any

#: The node in the discovery topics: ``<discovery>/<type>/<ns>_bridge/<object>/config``.
NODE_SUFFIX = "bridge"
#: Most alerts carried in the attributes; HA stores attributes in its database.
MAX_ALERTS = 20


def problem_topic(prefix: str) -> str:
    return f"{prefix}/bridge/problem"


def heartbeat_topic(prefix: str) -> str:
    return f"{prefix}/bridge/heartbeat"


def _discovery_topic(discovery_prefix: str, ha_type: str, prefix: str, obj: str) -> str:
    return f"{discovery_prefix}/{ha_type}/{prefix}_{NODE_SUFFIX}/{obj}/config"


def discovery_topics(prefix: str, discovery_prefix: str) -> list[str]:
    return [
        _discovery_topic(discovery_prefix, "binary_sensor", prefix, "bridge_problem"),
        _discovery_topic(discovery_prefix, "sensor", prefix, "bridge_heartbeat"),
    ]


def device_block(prefix: str, app_version: str) -> dict[str, Any]:
    return {
        "identifiers": [f"{prefix}_modbus_bridge"],
        "name": "FranklinWH Modbus Bridge",
        "manufacturer": "franklinwh-modbus-bridge",
        "model": "Modbus Bridge",
        # The producer marker (namespace.is_ours) the clean-up relies on.
        "sw_version": f"Modbus Bridge v{app_version}",
    }


def discovery_configs(prefix: str, discovery_prefix: str,
                      app_version: str) -> dict[str, dict[str, Any]]:
    """``{discovery topic: config}`` for the two sensors."""
    device = device_block(prefix, app_version)
    problem_t, heartbeat_t = discovery_topics(prefix, discovery_prefix)
    return {
        problem_t: {
            "unique_id": f"{prefix}_bridge_problem",
            "name": "Bridge problem",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "state_topic": problem_topic(prefix),
            "value_template": "{{ 'ON' if value_json.problem else 'OFF' }}",
            "json_attributes_topic": problem_topic(prefix),
            "device": device,
        },
        heartbeat_t: {
            "unique_id": f"{prefix}_bridge_heartbeat",
            "name": "Bridge heartbeat",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            "state_topic": heartbeat_topic(prefix),
            "device": device,
        },
    }


def problem_payload(summary: dict[str, Any], open_alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """``{problem, level, open, alerts[]}``: every open alert, any gateway, newest first."""
    alerts = [
        {
            "id": a["id"], "gateway_id": a["gateway_id"], "code": a["code"],
            "kind": a["kind"], "severity": a["severity"], "title": a["title"],
            "message": a["message"], "opened_ts": a["opened_ts"], "count": a["count"],
            "resolved": a["resolved_ts"] is not None, "acked": a["acked_ts"] is not None,
        }
        for a in open_alerts[:MAX_ALERTS]
    ]
    return {
        "problem": summary["open"] > 0,
        "level": summary["level"],
        "open": summary["open"],
        "alerts": alerts,
    }
