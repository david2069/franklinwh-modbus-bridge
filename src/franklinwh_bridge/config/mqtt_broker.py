"""Which MQTT broker the publisher uses, and where each setting came from (#103).

Precedence, per field (host, port, username, password):

1. ``environment`` — the ``MQTT_*`` variable is set and not empty. A deployment
   setting (docker-compose sets ``MQTT_HOST=mosquitto``) wins, as it always has.
2. ``settings`` — the value saved in Settings → Home Assistant → MQTT Broker.
3. ``supervisor`` — as an HA add-on, the broker the Supervisor reports, used
   only when no host is set by 1 or 2.
4. ``default`` — ``localhost:1883``.

Before this, the environment's host *always* replaced the saved one — even when
``MQTT_HOST`` was unset, its ``localhost`` default did — so a broker saved in
Settings was never used, not even after a restart.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

ENV_VARS = {
    "host": "MQTT_HOST",
    "port": "MQTT_PORT",
    "username": "MQTT_USERNAME",
    "password": "MQTT_PASSWORD",
}

#: A stored host of "localhost" is the column default, not a choice: treat it as
#: unset so an add-on still finds the Supervisor's broker.
_UNSET_HOSTS = (None, "", "localhost")


def env_values() -> dict[str, Any]:
    """The MQTT_* variables that are set and not empty, typed."""
    out: dict[str, Any] = {}
    for field, var in ENV_VARS.items():
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        if field == "port":
            try:
                out[field] = int(raw)
            except ValueError:
                continue
        else:
            out[field] = raw
    return out


async def resolve_broker(
    stored: dict[str, Any],
    discover: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(config, sources)``: ``stored`` with the broker fields resolved.

    ``discover`` is the Supervisor lookup; it is only called when no host is set
    by the environment or Settings. ``sources`` names where each field came from.
    """
    config = dict(stored)
    sources: dict[str, str] = {}
    env = env_values()
    for field in ENV_VARS:
        if field in env:
            config[field] = env[field]
            sources[field] = "environment"
        elif field == "host":
            if stored.get("host") not in _UNSET_HOSTS:
                sources[field] = "settings"
        elif stored.get(field) not in (None, ""):
            sources[field] = "settings"

    if "host" not in sources and discover is not None:
        found = await discover()
        if found:
            for field in ENV_VARS:
                if field not in sources and found.get(field) is not None:
                    config[field] = found[field]
                    sources[field] = "supervisor"

    if "host" not in sources:
        config["host"] = "localhost"
        sources["host"] = "default"
    if "port" not in sources:
        config["port"] = config.get("port") or 1883
        sources["port"] = "default" if config["port"] == 1883 else "settings"
    for field in ("username", "password"):
        sources.setdefault(field, "default")
    return config, sources
