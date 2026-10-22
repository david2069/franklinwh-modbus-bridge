"""Alert delivery to Home Assistant (#324 §6, §11 D): the outbox worker.

The alert row is the record; this task only tells Home Assistant. Every
critical alert that still needs someone gets a row in ``alert_deliveries`` for
the ``ha`` channel, and a persistent notification in HA
(``notification_id = fwh_mb_alert_<id>``), whether or not any phone is set up.

- **Retries:** a failed call is retried with back-off (30 s doubling to
  15 min), held in memory, so owed deliveries go out at once after a restart.
  After 24 h the row is marked failed. A failed call never loses or blocks an
  alert: the bridge's own modal shows it regardless.
- **Acknowledged in the bridge:** the HA notification is dismissed. A dismiss
  in HA is not an acknowledgement; the bridge stays the record.
- **Channel off** (owner decision 2026-10-21; on by default): each new
  delivery is recorded ``skipped``, "HA channel off". The modal still shows.
- **No HA configured:** recorded ``skipped``, "no Home Assistant configured".

Every delivery is also a WARNING line in the app log, delivered or not.
Warnings are not sent (spec §6: critical alerts only).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

from franklinwh_bridge import workers
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store.db import get_app_config, set_app_config

if TYPE_CHECKING:
    from franklinwh_bridge.gateway.ha import HaRegistry

logger = logging.getLogger(__name__)

CHANNEL_HA = "ha"
#: app_config key: "on" (default) or "off".
HA_CHANNEL_KEY = "alerts.ha_channel"
INTERVAL_S = 5
WORKER_NAME = "alert_delivery"
BACKOFF_FIRST_S = 30
BACKOFF_MAX_S = 900
GIVE_UP_AFTER_S = 24 * 3600
NOTIFICATION_TITLE = "FranklinWH Modbus Bridge"


def notification_id(alert_id: int) -> str:
    return f"fwh_mb_alert_{alert_id}"


def site_time(ts: float) -> str:
    """The bridge's own (site) time, with its zone: alerts show site time (spec §8)."""
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def notification_text(alert: dict[str, Any]) -> tuple[str, str]:
    """``(title, message)`` for HA's persistent notification."""
    where = f" ({alert['gateway_id']})" if alert.get("gateway_id") else ""
    count = f" ×{alert['count']}" if (alert.get("count") or 1) > 1 else ""
    message = (f"**{alert['title']}**{where}{count}, {site_time(alert['opened_ts'])}\n\n"
               f"{alert['message']}\n\n"
               "Open the bridge to see what to do and to acknowledge it.")
    return f"{NOTIFICATION_TITLE}: {alert['title']}", message


async def ha_channel_on(db: aiosqlite.Connection) -> bool:
    return (await get_app_config(db, HA_CHANNEL_KEY, "on")) != "off"


async def set_ha_channel(db: aiosqlite.Connection, on: bool) -> None:
    await set_app_config(db, HA_CHANNEL_KEY, "on" if on else "off")
    log = logger.info if on else logger.warning
    log("Alerts: Home Assistant notifications switched %s%s", "on" if on else "off",
        "" if on else "; critical alerts reach only the bridge UI")


class AlertDelivery:
    """Sends critical alerts to HA as persistent notifications, and dismisses them."""

    def __init__(self, db: aiosqlite.Connection, ha_registry: HaRegistry,
                 interval_s: float = INTERVAL_S) -> None:
        self._db = db
        self._ha = ha_registry
        self._interval = interval_s
        #: alert id -> (failures so far, next attempt ts)
        self._backoff: dict[int, tuple[int, float]] = {}
        #: alert id -> next dismiss attempt ts, after a failed dismiss
        self._dismiss_due: dict[int, float] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        workers.register(WORKER_NAME, "Alert delivery", self._interval, task=self._task)

    async def stop(self) -> None:
        workers.unregister(WORKER_NAME)
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                workers.beat(WORKER_NAME)
                try:
                    await self.run_once()
                except Exception:  # one bad pass must not end delivery
                    logger.exception("Alert delivery pass failed")
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass

    async def run_once(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        await self._create_rows()
        await self._send_pending(now)
        await self._dismiss_acked(now)

    async def _create_rows(self) -> None:
        on = await ha_channel_on(self._db)
        configured = self._ha.primary() is not None
        for a in await alert_store.deliveries_to_create(self._db, CHANNEL_HA):
            if not on:
                reason = "HA channel off"
            elif not configured:
                reason = "no Home Assistant configured"
            else:
                await alert_store.add_delivery(self._db, a["id"], CHANNEL_HA)
                continue
            await alert_store.add_delivery(self._db, a["id"], CHANNEL_HA, "skipped", reason)
            logger.warning("Alert %d %s not sent to Home Assistant: %s", a["id"], a["code"],
                           reason)

    async def _send_pending(self, now: float) -> None:
        for a in await alert_store.deliveries_in(self._db, CHANNEL_HA, "pending"):
            failures, due = self._backoff.get(a["id"], (0, 0.0))
            if now < due:
                continue
            inst = self._ha.primary()
            if a["acked_ts"] is not None:  # acknowledged before it went out
                await alert_store.mark_delivery(
                    self._db, a["id"], CHANNEL_HA, "skipped", attempted=False,
                    error="acknowledged before delivery", now=now)
                self._backoff.pop(a["id"], None)
                continue
            if inst is None:
                res = {"ok": False, "error": "no Home Assistant configured"}
            else:
                title, message = notification_text(a)
                res = await inst.call_action("persistent_notification", "create", {
                    "notification_id": notification_id(a["id"]),
                    "title": title, "message": message})
            if res["ok"]:
                await alert_store.mark_delivery(self._db, a["id"], CHANNEL_HA, "delivered",
                                                now=now)
                self._backoff.pop(a["id"], None)
                logger.warning("Alert %d %s sent to Home Assistant (%s): %s", a["id"],
                               a["code"], inst.name if inst else "?", a["message"])
                continue
            failures += 1
            if now - a["opened_ts"] >= GIVE_UP_AFTER_S:
                await alert_store.mark_delivery(self._db, a["id"], CHANNEL_HA, "failed",
                                                error=res["error"], now=now)
                self._backoff.pop(a["id"], None)
                logger.warning("Alert %d %s: gave up sending to Home Assistant after "
                               "%d tries: %s", a["id"], a["code"], a["attempts"] + 1,
                               res["error"])
                continue
            await alert_store.mark_delivery(self._db, a["id"], CHANNEL_HA, "pending",
                                            error=res["error"], now=now)
            delay = min(BACKOFF_FIRST_S * 2 ** (failures - 1), BACKOFF_MAX_S)
            self._backoff[a["id"]] = (failures, now + delay)
            logger.warning("Alert %d %s not yet sent to Home Assistant (try %d, next in "
                           "%d s): %s", a["id"], a["code"], a["attempts"] + 1, delay,
                           res["error"])

    async def _dismiss_acked(self, now: float) -> None:
        """Acknowledged in the bridge: take the notification down in HA."""
        for a in await alert_store.deliveries_in(self._db, CHANNEL_HA, "delivered"):
            if a["acked_ts"] is None or now < self._dismiss_due.get(a["id"], 0.0):
                continue
            inst = self._ha.primary()
            if inst is None:
                continue
            res = await inst.call_action("persistent_notification", "dismiss",
                                         {"notification_id": notification_id(a["id"])})
            if not res["ok"]:
                self._dismiss_due[a["id"]] = now + BACKOFF_MAX_S
                continue
            self._dismiss_due.pop(a["id"], None)
            await alert_store.mark_delivery(self._db, a["id"], CHANNEL_HA, "dismissed",
                                            attempted=False)
            logger.info("Alert %d acknowledged by %s; HA notification dismissed",
                        a["id"], a["acked_by"])
