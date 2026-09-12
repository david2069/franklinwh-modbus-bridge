"""Home Assistant Supervisor API — MQTT broker auto-discovery.

Running as an add-on, the broker details are already known to the Supervisor:
whoever set up the Mosquitto add-on configured them once. Asking the user to
retype host/port/username/password into a second form is both friction and a
way to get it subtly wrong.

Only used when ``detect_environment() == "ha_addon"`` and a SUPERVISOR_TOKEN is
present. Everywhere else (docker, dev) this is inert and MQTT stays manual, so
a non-addon install behaves exactly as before.

Discovered values sit BELOW explicit configuration in precedence: an operator
who typed a broker address meant it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SUPERVISOR_URL = "http://supervisor"
_TIMEOUT_S = 5.0


def supervisor_token() -> str | None:
    """The Supervisor API token, if we're running under it."""
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")


async def discover_mqtt() -> dict[str, Any] | None:
    """Ask the Supervisor for the configured MQTT service.

    Returns ``{host, port, username, password, tls}`` or None when unavailable —
    not an add-on, no MQTT service configured, or the call failed. Never raises:
    the bridge must start regardless, just without auto-config.
    """
    token = supervisor_token()
    if not token:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(
                f"{_SUPERVISOR_URL}/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 400:
            # Documented response when no MQTT service is registered.
            logger.info("Supervisor has no MQTT service configured — MQTT stays manual")
            return None
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
    except Exception as exc:  # pragma: no cover - network/env dependent
        logger.info("Supervisor MQTT discovery unavailable (%s) — MQTT stays manual", exc)
        return None

    if not data.get("host"):
        return None

    found = {
        "host": data.get("host"),
        "port": int(data.get("port") or 1883),
        "username": data.get("username") or None,
        "password": data.get("password") or None,
        "tls": bool(data.get("ssl", False)),
    }
    logger.info("MQTT auto-configured from Supervisor: %s:%s", found["host"], found["port"])
    return found
