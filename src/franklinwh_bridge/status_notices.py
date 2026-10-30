"""Status notices to Home Assistant: bridge started and stopping, gateway paused and resumed.

Owner request (2026-10-30), modelled on the Energipays bridge's started/stopped
notifications. Unlike alerts (alert_delivery.py) these are information, not a
record: one attempt each, no retries, no row. Every notice is an INFO line in the
app log, sent or not.

- **Where:** a persistent notification in the primary Home Assistant. The bridge's
  own notice replaces the last one (``fwh_mb_bridge_status``); each gateway has one
  of its own (``fwh_mb_gateway_<id>``), so they don't pile up.
- **Switch:** ``notify.status_notices`` in app_config, "on" (default) or "off".
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiosqlite

from franklinwh_bridge.alert_delivery import NOTIFICATION_TITLE, site_time
from franklinwh_bridge.store.db import get_app_config, set_app_config

if TYPE_CHECKING:
    from franklinwh_bridge.gateway.ha import HaRegistry

logger = logging.getLogger(__name__)

#: app_config key: "on" (default) or "off".
STATUS_NOTICES_KEY = "notify.status_notices"
BRIDGE_NOTIFICATION_ID = "fwh_mb_bridge_status"
#: A notice must never hold up startup or shutdown.
SEND_TIMEOUT_S = 5.0


def gateway_notification_id(gateway_id: str) -> str:
    return f"fwh_mb_gateway_{gateway_id}"


async def status_notices_on(db: aiosqlite.Connection) -> bool:
    return (await get_app_config(db, STATUS_NOTICES_KEY, "on")) != "off"


async def set_status_notices(db: aiosqlite.Connection, on: bool) -> None:
    await set_app_config(db, STATUS_NOTICES_KEY, "on" if on else "off")
    logger.info("Status notices to Home Assistant switched %s", "on" if on else "off")


def started_text(version: str, polling: list[dict], idle: list[dict]) -> tuple[str, str]:
    """``polling``: running gateways (name, poll_interval); ``idle``: the rest (name)."""
    n = len(polling)
    if n:
        each = ", ".join(f"{g['name']} (every {g['poll_interval']} s)" for g in polling)
        lines = [f"Bridge is up (v{version}): polling {n} gateway{'s' if n != 1 else ''}: {each}."]
    else:
        lines = [f"Bridge is up (v{version}): no gateway is polling."]
    if idle:
        lines.append("Not polling: " + ", ".join(g["name"] for g in idle) + ".")
    return f"{NOTIFICATION_TITLE}: started", "\n\n".join(lines)


def stopping_text() -> tuple[str, str]:
    return (f"{NOTIFICATION_TITLE}: stopped",
            "Bridge is shutting down: polling stops and any battery command is released.")


def paused_text(name: str, by: str) -> tuple[str, str]:
    return (f"{NOTIFICATION_TITLE}: {name} paused",
            f"Polling of {name} is paused (Stop, by {by}). It stays paused across restarts "
            "until someone presses Start.")


def resumed_text(name: str, by: str) -> tuple[str, str]:
    return (f"{NOTIFICATION_TITLE}: {name} polling",
            f"Polling of {name} has started (by {by}).")


async def send(db: aiosqlite.Connection, ha: HaRegistry | None, notification_id: str,
               title: str, message: str, now: float) -> bool:
    """Log the notice, then send it to Home Assistant once. Never raises."""
    try:
        reason = None
        inst = ha.primary() if ha is not None else None
        if not await status_notices_on(db):
            reason = "status notices off"
        elif inst is None:
            reason = "no Home Assistant configured"
        if reason is None:
            res = await asyncio.wait_for(
                inst.call_action("persistent_notification", "create", {
                    "notification_id": notification_id, "title": title,
                    "message": f"{message}\n\n{site_time(now)}"}),
                SEND_TIMEOUT_S)
            reason = None if res["ok"] else res["error"]
        if reason is None:
            logger.info("Status notice sent to Home Assistant (%s): %s", inst.name, message)
            return True
        logger.info("Status notice not sent to Home Assistant (%s): %s", reason, message)
    except Exception as exc:  # a notice must never break startup, shutdown or a route
        logger.info("Status notice not sent to Home Assistant (%s): %s",
                    str(exc) or type(exc).__name__, message)
    return False
