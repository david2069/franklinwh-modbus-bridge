"""Home Assistant Supervisor API — MQTT broker and timezone auto-discovery.

Running as an add-on, both are already known to the Supervisor: whoever set up
the Mosquitto add-on configured the broker once, and the host's timezone is a
Supervisor-level setting. Asking the user to retype either into a second form is
both friction and a way to get it subtly wrong.

**Resolved here in Python, deliberately, rather than via bashio in run.sh.**
FWHAI shipped the bashio version and found that on a real install every such
call returned ``ERROR: Unable to access the API, forbidden`` — while the
identical token worked from Python in the same boot. The failure is silent:
``if bashio::info.timezone > /dev/null 2>&1`` cannot tell "forbidden" from "not
configured", so it falls through and the container stays on UTC. That is not
cosmetic here — schedule triggers, TOU blocks, demand and export windows are all
evaluated in local wall-clock, so a Sydney site runs every automation ten hours
out, and ``config/clock.py`` then records UTC as the expected zone at first run.

Only used when ``detect_environment() == "ha_addon"`` and a SUPERVISOR_TOKEN is
present. Everywhere else (docker, dev) this is inert, so a non-addon install
behaves exactly as before.

Discovered values sit BELOW explicit configuration in precedence: an operator
who typed a broker address, or set TZ, meant it.
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


async def discover_timezone() -> str | None:
    """Ask the Supervisor for the host timezone (IANA name), or None.

    Never raises and never guesses: outside an add-on there is no Supervisor,
    which is a normal state rather than a failure. Returns None so the caller
    keeps whatever TZ it already had.
    """
    token = supervisor_token()
    if not token:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(
                f"{_SUPERVISOR_URL}/info",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        tz = ((resp.json() or {}).get("data") or {}).get("timezone")
    except Exception as exc:  # pragma: no cover - network/env dependent
        logger.warning(
            "Supervisor timezone lookup failed (%s) — keeping TZ=%s. Schedules and "
            "TOU windows run on this clock, so check it if automations fire at the "
            "wrong hour.", exc, os.environ.get("TZ") or "unset",
        )
        return None

    if not tz or tz == "null":
        return None
    return str(tz)


async def apply_timezone() -> str | None:
    """Set ``TZ`` from the Supervisor when the user has not set it themselves.

    Returns the applied zone, or None if nothing changed. An operator who set TZ
    explicitly meant it — the Supervisor value is a default, not an override.
    """
    existing = os.environ.get("TZ")
    if existing and existing != "UTC":
        return None

    tz = await discover_timezone()
    if not tz or tz == existing:
        return None

    os.environ["TZ"] = tz
    try:
        import time as _time

        _time.tzset()  # make the change visible to datetime.now() immediately
    except AttributeError:  # pragma: no cover - non-POSIX
        pass
    logger.info("Timezone auto-configured from Supervisor: %s", tz)
    return tz
