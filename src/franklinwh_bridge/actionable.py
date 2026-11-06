"""Phone notifications with buttons (#296 item 48), through franklinwh-ha-notify.

One :class:`~franklinwh_ha_notify.Notifier` per bridge process, over every enabled Home
Assistant instance in ``ha_instances``. Sends and taps are recorded in ``notify_ledger``;
open requests are kept in ``notify_requests`` (both created by migration 66), so a tap
after a restart still resolves. Taps arrive over each instance's own authenticated
WebSocket, opened by the library (owner, 2026-11-06: its own connection); nothing on the
bridge accepts an unauthenticated call.

**Push notifications switch.** One switch for phones and Home Assistant notifications
(owner, 2026-11-06). It is the critical-alert channel's existing setting
(``alerts.ha_channel``), so an install keeps its choice. The per-device **Test** is the one
send that ignores it, to check a device before or after saving it.

A device is one ``notify.*`` service on one instance, saved with an alias
(``notify_devices``). Only ``notify.mobile_app_*`` services show buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import time
from typing import Any

import aiosqlite
from franklinwh_ha_notify import (
    NO,
    YES,
    Button,
    Event,
    HAInstance,
    Ledger,
    Notifier,
    Requests,
    SendResult,
    Target,
    Templates,
    discover,
    friendly,
    send,
)

from franklinwh_bridge import alert_delivery
from franklinwh_bridge.alert_delivery import NOTIFICATION_TITLE
from franklinwh_bridge.config.supervisor import (
    SUPERVISOR_CORE_URL,
    is_supervisor_instance,
    supervisor_token,
)
from franklinwh_bridge.store.db import (
    get_app_config,
    get_ha_instances,
    get_notify_devices,
    set_app_config,
)

logger = logging.getLogger(__name__)

EVENT_TEST = "test"
EVENT_TEST_BUTTONS = "test_buttons"
EVENT_BROADCAST = "broadcast"
#: How long Test with buttons waits for a tap.
TEST_BUTTONS_TTL_S = 120
#: app_config key: the last Test with buttons, JSON, kept across restarts.
LAST_TEST_KEY = "notify.last_test_buttons"

_TEMPLATES = Templates()
_TEMPLATES.register(
    Event(EVENT_TEST, f"{NOTIFICATION_TITLE}: test",
          "Test from Settings → Notifications. Nothing is wrong."),
    Event(EVENT_TEST_BUTTONS, f"{NOTIFICATION_TITLE}: test",
          "Test with buttons from Settings → Notifications. Tap Yes or No."),
    Event(EVENT_BROADCAST, "{title}", "{message}", ("title", "message")),
)

_DUPLICATE = re.compile(r"^(.*)_(\d+)$")
KINDS = ("phone", "tablet", "tv", "speaker", "other")


def device_kind(service: str, name: str = "") -> str:
    """phone, tablet, tv, speaker or other, from the notify service and HA's name for it.
    The Direct Connect bridge's rule (notify_engine.kind_of), so both bridges agree. The
    user can change it in the Add dialog."""
    words = set(re.split(r"[^a-z0-9]+", f"{service} {name}".lower()))
    for kind, hints in (("tablet", {"tablet", "ipad", "tab"}),
                        ("tv", {"tv", "shield", "chromecast", "firestick"}),
                        ("speaker", {"speaker", "echo", "alexa", "homepod", "sonos", "nest"})):
        if words & hints:
            return kind
    return "phone" if service.removeprefix("notify.").startswith("mobile_app_") else "other"


def mark_duplicates(services: list[str]) -> dict[str, str]:
    """``{"mobile_app_x_2": "mobile_app_x"}``: a numbered service whose base also exists.
    HA adds ``_2`` when a phone is set up again; the one without the suffix is usually right."""
    have = set(services)
    out = {}
    for s in services:
        m = _DUPLICATE.match(s)
        if m and m.group(1) in have:
            out[s] = m.group(1)
    return out


def _ha_instance(row: dict) -> HAInstance:
    """The library's view of one ``ha_instances`` row. The add-on's own instance reads
    the Supervisor token on every call: it changes each time the add-on starts and is
    never stored."""
    if row.get("token"):
        token: Any = row["token"]
    elif is_supervisor_instance(row) or row["base_url"].rstrip("/") == SUPERVISOR_CORE_URL:
        token = lambda: supervisor_token() or ""  # noqa: E731
    else:
        token = ""
    return HAInstance(row["id"], row["base_url"], token=token, name=row.get("name") or "")


class Actionable:
    """The bridge's one Notifier, its devices and the Push notifications switch."""

    def __init__(self, db: aiosqlite.Connection, db_path: str) -> None:
        self._db = db
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self.notifier: Notifier | None = None
        self._enabled = True
        self._instance_rows: dict[str, dict] = {}
        #: Test with buttons, by request id: {device_id, alias, sent_at, result, answered_at}.
        self.tests: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        #: start and stop never overlap: a stop during start would close the database
        #: connection under the worker thread still loading requests.
        self._life = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────
    async def _instances(self) -> dict[str, HAInstance]:
        rows = [r for r in await get_ha_instances(self._db)
                if r.get("enabled") and r.get("base_url")]
        self._instance_rows = {r["id"]: r for r in rows}
        return {r["id"]: _ha_instance(r) for r in rows}

    async def start(self) -> None:
        """Build the Notifier and open one tap listener per enabled instance. Never raises:
        notifications must never stop the bridge."""
        async with self._life:
            await self._start()

    async def _start(self) -> None:
        try:
            self._enabled = await alert_delivery.ha_channel_on(self._db)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            requests = await asyncio.to_thread(Requests.persistent, self._conn, create=False)
            self.notifier = Notifier(await self._instances(),
                                     Ledger(self._conn, create=False), _TEMPLATES,
                                     enabled=lambda: self._enabled, requests=requests)
            self.notifier.listen()
            logger.info("Notifications: listening for button taps on %d Home Assistant "
                        "instance(s)", len(self.notifier.instances))
        except Exception as exc:
            logger.error("Notifications could not start: %s", exc)
            self.notifier = None

    async def stop(self) -> None:
        async with self._life:
            for task in list(self._tasks):
                task.cancel()
            if self.notifier is not None:
                await self.notifier.close()
                self.notifier = None
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def sync_instances(self) -> None:
        """After an HA instance is added, changed or removed: start, re-sign or stop its
        tap listener. No restart needed. Never raises."""
        if self.notifier is None:
            return
        try:
            await self.notifier.set_instances(await self._instances())
        except Exception as exc:
            logger.warning("HA instances changed, but the tap listeners could not follow: %s",
                           exc)

    # ── the switch ─────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    async def set_enabled(self, on: bool) -> None:
        await alert_delivery.set_ha_channel(self._db, on)
        self._enabled = on
        logger.info("Push notifications switched %s", "on" if on else "off")

    # ── devices and targets ───────────────────────────────────
    async def targets(self, device_ids: list[str] | None = None) -> list[Target]:
        """Enabled devices on enabled instances; or, with ``device_ids``, exactly those
        devices (on or off) on enabled instances."""
        live = self.notifier.instances if self.notifier else {}
        return [Target(d["instance_id"], d["service"], d["alias"])
                for d in await get_notify_devices(self._db)
                if ((d["id"] in device_ids) if device_ids is not None else d["enabled"])
                and d["instance_id"] in live]

    async def discover(self, instance_id: str) -> list[dict]:
        """Every ``notify.*`` service on one enabled instance, phones first, with what tells
        near-duplicates apart, as the Direct Connect bridge shows them: HA's friendly name
        for the device (from its ``notify.*`` entity, else one made from the service),
        ``kind``, ``last_updated`` (the only sign of which of ``x`` and ``x_2`` is live),
        ``duplicate_of`` and ``saved`` (already a device)."""
        if self.notifier is None or instance_id not in self.notifier.instances:
            raise LookupError("HA instance not found or switched off")
        ha = self.notifier.instances[instance_id]
        found = await discover(ha)
        meta: dict[str, dict] = {}
        try:  # metadata only: a service with no matching entity is still a target
            async with ha.client() as c:
                r = await c.get("/api/states")
                r.raise_for_status()
                for st in r.json():
                    eid = st.get("entity_id", "")
                    if eid.startswith("notify."):
                        meta[eid.removeprefix("notify.")] = {
                            "name": (st.get("attributes") or {}).get("friendly_name") or "",
                            "last_updated": st.get("last_updated") or st.get("last_changed")}
        except Exception as exc:
            logger.debug("Discover: no notify entity metadata from %s: %s", instance_id, exc)
        dups = mark_duplicates([t.service for t in found])
        saved = {d["service"] for d in await get_notify_devices(self._db)
                 if d["instance_id"] == instance_id}
        out = []
        for t in found:
            m = meta.get(t.service, {})
            name = m.get("name") or friendly(t.service)
            out.append({"service": t.service, "friendly_name": name, "buttons": t.buttons,
                        "kind": device_kind(t.service, name),
                        "last_updated": m.get("last_updated"),
                        "duplicate_of": dups.get(t.service),
                        "saved": t.service in saved})
        return out

    async def test(self, device: dict) -> SendResult:
        """One plain message straight to the device, or to an unsaved ``{instance_id,
        service}`` from the Add dialog. Sends with the switch off, so a device can be
        checked before it is saved or notifications are switched on. Recorded."""
        if self.notifier is None:
            return SendResult()
        title, message = _TEMPLATES.render(EVENT_TEST)
        target = Target(device["instance_id"], device["service"],
                        device.get("alias") or device["service"])
        res = await send(self.notifier.instances, [target], title, message)
        await self.notifier.ledger.arecord(
            "SENT" if res.delivered else "ERROR", EVENT_TEST, status=res.summary,
            title=title, message=message, delivered=res.delivered, total=res.total,
            detail=[d.__dict__ for d in res.deliveries])
        return res

    async def test_buttons(self, device: dict) -> tuple[str | None, str | None]:
        """Send Yes and No to one phone and wait for the tap in the background.
        ``(request_id, None)``, or ``(None, why not)``. Follows the switch, like every
        notification with buttons."""
        if self.notifier is None:
            return None, "Notifications are not running"
        if not self._enabled:
            return None, "Push notifications are off"
        target = Target(device["instance_id"], device["service"], device["alias"])
        if not target.buttons:
            return None, "Only a notify.mobile_app_* service shows buttons"
        req = await self.notifier.ask(EVENT_TEST_BUTTONS, [target],
                                      [Button(YES, "Yes"), Button(NO, "No")],
                                      ttl_s=TEST_BUTTONS_TTL_S,
                                      meta={"device_id": device["id"]})
        if req is None:
            return None, "The phone could not be reached: see Recent notifications"
        self.tests[req.id] = {"device_id": device["id"], "alias": device["alias"],
                              "sent_at": time.time(), "result": "waiting",
                              "answered_at": None}
        task = asyncio.create_task(self._await_test(req))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return req.id, None

    async def _await_test(self, req) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            ans = await self.notifier.wait(req, TEST_BUTTONS_TTL_S)
            row = self.tests[req.id]
            row["result"] = "no reply" if ans.timed_out else ans.verb.lower()
            row["answered_at"] = time.time()
            await set_app_config(self._db, LAST_TEST_KEY, json.dumps(row))
            logger.info("Test with buttons to %s: %s", row["alias"],
                        "no reply within 120 s" if ans.timed_out
                        else f"round trip works, tapped {ans.verb.title()}")

    async def last_test(self) -> dict | None:
        """The latest finished Test with buttons, kept across restarts; or None."""
        raw = await get_app_config(self._db, LAST_TEST_KEY, "")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    async def broadcast(self, message: str, title: str | None = None) -> SendResult:
        """One message to every enabled device. Follows the switch."""
        if self.notifier is None:
            return SendResult()
        return await self.notifier.notify(EVENT_BROADCAST, await self.targets(),
                                          title=title or NOTIFICATION_TITLE, message=message)

    # ── status ────────────────────────────────────────────────
    def listener_status(self) -> dict[str, dict]:
        """``{instance_id: {name, connected, error}}`` for every tap listener."""
        if self.notifier is None:
            return {}
        out = {}
        for iid, st in self.notifier.listener_status().items():
            row = self._instance_rows.get(iid, {})
            out[iid] = {"name": row.get("name") or iid, **st}
        return out

    async def recent(self, limit: int = 100) -> list[dict]:
        if self.notifier is None:
            return []
        return await asyncio.to_thread(self.notifier.ledger.recent, limit)
