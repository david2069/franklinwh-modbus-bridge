"""Multiple Home Assistant instances → one Bridge (inbound entity access).

An `HaRegistry` holds N `HaInstance` clients (one flagged default). Each instance
maintains a **WebSocket** connection to its HA (``/api/websocket``): it seeds the
cache with ``get_states`` then subscribes to ``state_changed`` events so entity
values update in near-real-time. The states are exposed as
``ha:<instance_id>:<entity_id>`` automation condition sensors (merged into the
sensor snapshot the engine + /api/sensors evaluate).

If the WebSocket drops (or can't connect), the instance reconnects with backoff
and falls back to a one-shot REST ``GET /api/states`` refresh in between so the
cache doesn't go stale. REST ``refresh()`` also backs the config test-connection
probe. This is the inbound direction (read HA entity states), distinct from the
existing outbound MQTT discovery.

State coercion: HA states are strings. `unavailable`/`unknown`/empty → None (so
conditions fail closed on a stale entity); `on`/`off` (+ true/false) → bool;
numeric → float; otherwise the raw string (enum) — mirroring the sensor
evaluator's own coercion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect

from franklinwh_bridge.config.supervisor import SUPERVISOR_CORE_URL, supervisor_token
from franklinwh_bridge.store.db import (
    get_all_exposed_entities,
    get_ha_instances,
    set_entity_exposed,
)

logger = logging.getLogger(__name__)


def _domain(entity_id: str) -> str:
    """HA entity domain — the part before the first dot (``sensor.foo`` → ``sensor``)."""
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


DEFAULT_POLL_INTERVAL_S = 30
#: WebSocket reconnect backoff bounds (seconds).
_WS_BACKOFF_START_S = 1.0
_WS_BACKOFF_MAX_S = 30.0
#: HA get_states payloads can be large on big installs — lift the frame cap.
_WS_MAX_SIZE = 16 * 1024 * 1024

_UNAVAILABLE = frozenset({"unavailable", "unknown", "none", ""})
_TRUE = frozenset({"on", "true", "home", "open", "locked"})
_FALSE = frozenset({"off", "false", "away", "closed", "unlocked"})


class HaAuthError(Exception):
    """HA rejected the WebSocket access token."""


def coerce_state(state: Any) -> float | bool | str | None:
    """HA string state → number / bool / str, or None if unavailable."""
    if state is None:
        return None
    s = str(state).strip()
    low = s.lower()
    if low in _UNAVAILABLE:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return s


class HaInstance:
    """One HA connection: keeps an entity-state cache live over a WebSocket,
    with REST ``/api/states`` as the seed/fallback."""

    def __init__(self, cfg: dict, *, timeout: float = 10.0) -> None:
        self.id: str = cfg["id"]
        self.name: str = cfg["name"]
        self.base_url: str = str(cfg["base_url"]).rstrip("/")
        self.token: str | None = cfg.get("token")
        if not self.token and self.base_url == SUPERVISOR_CORE_URL:
            # The add-on's own HA, via the Supervisor proxy. Its token is issued
            # per add-on start, so it's read from the environment, never stored.
            self.token = supervisor_token()
        self._timeout = timeout
        self.connected = False
        self.last_error: str | None = None
        #: "ws" once a live subscription is established, else "rest"/"init".
        self.transport = "init"
        #: Allowlist — only these entity_ids surface as ha:* condition sensors.
        self.exposed: set[str] = set()
        # entity_id -> {"state": str, "attributes": {...}}
        self._states: dict[str, dict] = {}

    def set_exposed(self, entity_id: str, exposed: bool) -> None:
        if exposed:
            self.exposed.add(entity_id)
        else:
            self.exposed.discard(entity_id)

    # ── URLs / headers ────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @property
    def ws_url(self) -> str:
        """``/api/websocket`` URL derived from base_url (http→ws, https→wss)."""
        url = self.base_url
        if url.startswith("https://"):
            url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://") :]
        return url + "/api/websocket"

    # ── cache mutation (pure, unit-tested) ────────────────────
    def _seed_states(self, states: list) -> None:
        """Replace the cache from a get_states / REST result list."""
        self._states = {
            e["entity_id"]: {"state": e.get("state"), "attributes": e.get("attributes", {})}
            for e in states
            if isinstance(e, dict) and "entity_id" in e
        }

    def _apply_state_changed(self, data: dict) -> None:
        """Apply one ``state_changed`` event's data to the cache."""
        eid = data.get("entity_id")
        if not eid:
            return
        new = data.get("new_state")
        if new is None:  # entity removed
            self._states.pop(eid, None)
        else:
            self._states[eid] = {
                "state": new.get("state"),
                "attributes": new.get("attributes", {}),
            }

    # ── outbound control (automation actions) ─────────────────
    async def notify(self, service: str, title: str, message: str) -> dict:
        """Send a one-way notification via an HA ``notify.*`` service.

        Distinct from :meth:`call_service` because a notification is NOT an
        entity operation: ``notify.mobile_app_x`` takes ``title``/``message`` in
        the body and no entity_id. Passing one makes HA reject the call.

        One-way by design — HA is told, nothing comes back. Actionable
        (two-way) notifications need a callback route and are a separate
        feature; see the notifications backlog.
        """
        name = (service or "").split(".", 1)[-1].strip()
        if not name:
            return {"ok": False, "error": f"bad notify service: {service!r}"}
        url = f"{self.base_url}/api/services/notify/{name}"
        payload: dict = {"message": message or ""}
        if title:
            payload["title"] = title
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
            return {"ok": True, "error": None}
        except Exception as exc:
            err = str(exc) or type(exc).__name__
            logger.warning("HA %s notify.%s failed: %s", self.name, name, err)
            return {"ok": False, "error": err}

    async def list_notify_services(self) -> list[str]:
        """Available ``notify.*`` service names from HA's /api/services.

        Notify targets are services, not entities, so they never appear in the
        entity cache the rest of this class is built around — they have to be
        asked for separately.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self.base_url}/api/services", headers=self._headers())
                resp.raise_for_status()
                domains = resp.json() or []
        except Exception as exc:
            logger.debug("HA %s list services failed: %s", self.name, exc)
            return []
        for d in domains:
            if d.get("domain") == "notify":
                return sorted((d.get("services") or {}).keys())
        return []

    async def call_service(
        self, entity_id: str, service: str, data: dict | None = None
    ) -> dict:
        """Call an HA service on ``entity_id`` via REST
        (``POST /api/services/{domain}/{service}``). ``service`` is a bare name
        in the entity's domain (e.g. ``turn_on``, ``select_option``); ``data``
        carries extra fields (``{"option": ...}`` / ``{"value": ...}``).

        Returns ``{"ok": bool, "error": str|None}`` — never raises, so one bad
        action can't abort a schedule's dispatch."""
        domain = _domain(entity_id)
        if not domain or not service:
            return {"ok": False, "error": f"bad entity/service: {entity_id!r}/{service!r}"}
        url = f"{self.base_url}/api/services/{domain}/{service}"
        payload = {"entity_id": entity_id, **(data or {})}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
            return {"ok": True, "error": None}
        except Exception as exc:
            err = str(exc) or type(exc).__name__
            logger.warning("HA %s call_service %s.%s(%s) failed: %s",
                           self.name, domain, service, entity_id, err)
            return {"ok": False, "error": err}

    # ── REST (seed / fallback / test-connection) ──────────────
    async def refresh(self) -> None:
        """Pull /api/states into the cache over REST. Sets connected/last_error."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self.base_url}/api/states", headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
            self._seed_states(data)
            self.connected = True
            self.last_error = None
            self.transport = "rest"
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc) or type(exc).__name__
            logger.debug("HA %s refresh failed: %s", self.name, self.last_error)

    async def ws_call(self, command: dict) -> Any:
        """Run one WebSocket command on a short-lived connection; return its result.

        For admin reads the REST API doesn't offer (config entries, the entity
        registry). Raises HaAuthError on bad credentials, RuntimeError when HA
        answers with an error (e.g. the token may not run that command).
        """
        async with ws_connect(
            self.ws_url, open_timeout=self._timeout, max_size=_WS_MAX_SIZE
        ) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") == "auth_required":
                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                resp = json.loads(await ws.recv())
                if resp.get("type") != "auth_ok":
                    raise HaAuthError(resp.get("message") or "authentication failed")
            await ws.send(json.dumps({"id": 1, **command}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), self._timeout))
                if msg.get("type") == "result" and msg.get("id") == 1:
                    if not msg.get("success", False):
                        err = msg.get("error") or {}
                        raise RuntimeError(err.get("message") or err.get("code") or "failed")
                    return msg.get("result")

    # ── WebSocket live subscription ───────────────────────────
    async def run_live(self) -> None:
        """Maintain a live ``state_changed`` subscription, reconnecting forever
        with backoff. Cancel the task to stop. Between drops, a one-shot REST
        refresh keeps the cache warm."""
        backoff = _WS_BACKOFF_START_S
        while True:
            try:
                await self._live_session()
                backoff = _WS_BACKOFF_START_S  # clean server close → reconnect promptly
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc) or type(exc).__name__
                logger.debug("HA %s live session ended: %s", self.name, self.last_error)
            # Fallback so conditions have *some* data during the outage window.
            with contextlib.suppress(Exception):
                await self.refresh()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _WS_BACKOFF_MAX_S)

    async def _live_session(self) -> None:
        """One WebSocket lifecycle: auth → get_states seed → subscribe → consume."""
        async with ws_connect(
            self.ws_url,
            open_timeout=self._timeout,
            ping_interval=20,
            ping_timeout=20,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            # 1. Auth handshake (HA sends auth_required first).
            first = json.loads(await ws.recv())
            if first.get("type") == "auth_required":
                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                resp = json.loads(await ws.recv())
                if resp.get("type") != "auth_ok":
                    raise HaAuthError(resp.get("message") or "authentication failed")
            elif first.get("type") == "auth_invalid":
                raise HaAuthError(first.get("message") or "authentication failed")

            # 2. Seed with a full state snapshot, then subscribe to changes.
            await ws.send(json.dumps({"id": 1, "type": "get_states"}))
            await ws.send(
                json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"})
            )

            # 3. Consume: result#1 seeds the cache, events keep it live.
            async for raw in ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "result" and msg.get("id") == 1:
                    if not msg.get("success", True):
                        raise RuntimeError(f"get_states failed: {msg.get('error')}")
                    self._seed_states(msg.get("result") or [])
                    self.connected = True
                    self.last_error = None
                    self.transport = "ws"
                elif mtype == "event":
                    data = (msg.get("event") or {}).get("data") or {}
                    self._apply_state_changed(data)

    # ── views ─────────────────────────────────────────────────
    def values(self) -> dict[str, Any]:
        """Exposed entity states as `ha:<id>:<entity_id>` → coerced value."""
        return {
            f"ha:{self.id}:{eid}": coerce_state(s.get("state"))
            for eid, s in self._states.items()
            if eid in self.exposed
        }

    def catalog(self) -> list[dict]:
        """Sensor metadata for each EXPOSED entity (for /api/sensors dropdowns)."""
        out = []
        for eid, s in self._states.items():
            if eid not in self.exposed:
                continue
            attrs = s.get("attributes") or {}
            val = coerce_state(s.get("state"))
            kind = (
                "number"
                if isinstance(val, (int, float)) and not isinstance(val, bool)
                else ("bool" if isinstance(val, bool) else "enum")
            )
            fn = attrs.get("friendly_name") or eid
            out.append(
                {
                    "id": f"ha:{self.id}:{eid}",
                    "label": f"{self.name}: {fn}",
                    "unit": attrs.get("unit_of_measurement"),
                    "kind": kind,
                    "value": val,
                    "source": "ha",
                    "group": f"HA · {self.name}",
                    "instance": self.id,
                }
            )
        return out

    def browse(self) -> list[dict]:
        """ALL cached entities (ignores the allowlist) with an ``exposed`` flag —
        backs the HA Entities browser."""
        out = []
        for eid, s in self._states.items():
            attrs = s.get("attributes") or {}
            out.append(
                {
                    "instance": self.id,
                    "instance_name": self.name,
                    "entity_id": eid,
                    "friendly_name": attrs.get("friendly_name") or eid,
                    "domain": _domain(eid),
                    "state": s.get("state"),
                    "value": coerce_state(s.get("state")),
                    "unit": attrs.get("unit_of_measurement"),
                    # For select/input_select: the pickable options (for actions).
                    "options": attrs.get("options"),
                    "exposed": eid in self.exposed,
                }
            )
        return out


class HaRegistry:
    """Holds the configured HA instances; runs a live WS task per instance;
    exposes merged entity values."""

    def __init__(self, db: Any, *, poll_interval_s: int = DEFAULT_POLL_INTERVAL_S) -> None:
        self._db = db
        self._interval = poll_interval_s  # REST fallback cadence (unused while WS is up)
        self._instances: dict[str, HaInstance] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def load(self) -> None:
        """(Re)build the instance set from the ha_instances table (enabled only),
        seeding each instance's exposed-entity allowlist."""
        rows = await get_ha_instances(self._db)
        self._instances = {r["id"]: HaInstance(r) for r in rows if r.get("enabled")}
        exposed = await get_all_exposed_entities(self._db)
        for iid, inst in self._instances.items():
            inst.exposed = exposed.get(iid, set())

    def _spawn_tasks(self) -> None:
        for inst in self._instances.values():
            self._tasks[inst.id] = asyncio.create_task(inst.run_live())

    async def _cancel_tasks(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def start(self) -> None:
        await self.load()
        await self._refresh_all()  # immediate REST seed so data is present at once
        self._spawn_tasks()  # then WS takes over for live updates
        logger.info("HA registry started (%d instance(s), live)", len(self._instances))

    async def stop(self) -> None:
        await self._cancel_tasks()

    async def reload(self) -> None:
        """Reload config, reseed, and restart the live tasks (call after a CRUD change)."""
        await self._cancel_tasks()
        await self.load()
        await self._refresh_all()
        self._spawn_tasks()

    async def _refresh_all(self) -> None:
        if not self._instances:
            return
        await asyncio.gather(*(inst.refresh() for inst in self._instances.values()))

    def get(self, instance_id: str) -> HaInstance | None:
        return self._instances.get(instance_id)

    def entity_values(self) -> dict[str, Any]:
        """Merged `ha:<inst>:<entity>` → value across all instances."""
        merged: dict[str, Any] = {}
        for inst in self._instances.values():
            merged.update(inst.values())
        return merged

    def catalog(self) -> list[dict]:
        out: list[dict] = []
        for inst in self._instances.values():
            out.extend(inst.catalog())
        return out

    def browse(self) -> list[dict]:
        """All entities across all instances (with exposed flags) for the browser."""
        out: list[dict] = []
        for inst in self._instances.values():
            out.extend(inst.browse())
        return out

    def domains(self) -> list[str]:
        return sorted({r["domain"] for r in self.browse() if r["domain"]})

    async def set_exposed(self, instance_id: str, entity_id: str, exposed: bool) -> bool:
        """Persist an allowlist change and apply it in-memory immediately."""
        await set_entity_exposed(self._db, instance_id, entity_id, exposed)
        inst = self._instances.get(instance_id)
        if inst is not None:
            inst.set_exposed(entity_id, exposed)
        return exposed

    async def call_service(
        self, instance_id: str, entity_id: str, service: str, data: dict | None = None
    ) -> dict:
        """Route an automation action to the right HA instance. Returns
        ``{"ok": bool, "error": str|None}`` (never raises)."""
        inst = self._instances.get(instance_id)
        if inst is None:
            return {"ok": False, "error": f"HA instance {instance_id!r} not found"}
        return await inst.call_service(entity_id, service, data)

    async def notify(
        self, instance_id: str, service: str, title: str, message: str
    ) -> dict:
        """Route a notification to the right HA instance (never raises)."""
        inst = self._instances.get(instance_id)
        if inst is None:
            return {"ok": False, "error": f"HA instance {instance_id!r} not found"}
        return await inst.notify(service, title, message)

    async def notify_services(self) -> list[dict]:
        """Every notify target across all instances, for the action picker."""
        out: list[dict] = []
        for inst in self._instances.values():
            for name in await inst.list_notify_services():
                out.append({
                    "instance_id": inst.id,
                    "instance_name": inst.name,
                    "service": name,
                    "label": f"{name} ({inst.name})",
                })
        return out

    def status(self) -> list[dict]:
        return [
            {
                "id": i.id,
                "name": i.name,
                "base_url": i.base_url,
                "connected": i.connected,
                "transport": i.transport,
                "last_error": i.last_error,
                "entity_count": len(i._states),
            }
            for i in self._instances.values()
        ]
