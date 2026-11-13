"""Auto-notify triggers: phone notifications that fire on their own (owner, 2026-11-13).

The Direct Connect bridge's triggers (its ``notify_engine``), with the same rows, defaults,
Send to and wording, so both bridges notify alike: aGate offline / online, off-grid started /
ended, battery dispatch started / ended, SoC below / reaching a threshold, bridge started /
restarted / stopped, and the Scheduler group (run failed, missed, aborted, no reply).

Off-grid also says how long the battery lasts at the current home load. The FranklinWH app
estimates from historical usage; this uses the load at the moment it went off-grid.

Sends go through the bridge's one Notifier (``actionable``), so they follow the Push
notifications switch and are recorded in Recent notifications. Every trigger that fires is
also a WARNING line in the app log, delivered or not.

Config is one JSON value in ``app_config`` (``notify.triggers``): the master switch and, per
trigger, enabled, threshold and target. Evaluation is pure (:func:`evaluate` on a gateway's
previous and current points); delivery is :meth:`Triggers.fire`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import aiosqlite
from franklinwh_ha_notify import Event

from franklinwh_bridge.store.db import get_app_config, get_notify_devices, set_app_config

if TYPE_CHECKING:
    from franklinwh_bridge.actionable import Actionable

logger = logging.getLogger(__name__)

CONFIG_KEY = "notify.triggers"
#: app_config: the last clean stop ({ts, version}), read once by the next start.
STOP_KEY = "notify.bridge_last_stop"

#: The trigger catalogue, as the Direct Connect bridge's. ``threshold`` present: the UI
#: shows a number field. ``group``: a heading above the first row of the group.
DEFAULT_TRIGGERS: dict[str, dict] = {
    "aGate_offline": {"enabled": True, "label": "aGate offline / online"},
    "off_grid": {"enabled": True, "label": "Off-grid started / ended"},
    "dispatch": {"enabled": True, "label": "Battery dispatch (VPP / force) started / ended"},
    "soc_low": {"enabled": False, "label": "Battery SoC drops below", "threshold": 20},
    "soc_full": {"enabled": False, "label": "Battery SoC reaches", "threshold": 100},
    "bridge": {"enabled": True, "label": "Bridge started / restarted / stopped"},
    "schedule_failed": {"enabled": True, "label": "Run failed", "group": "Scheduler"},
    "schedule_missed": {"enabled": True, "label": "Run missed", "group": "Scheduler"},
    "schedule_aborted": {"enabled": False, "group": "Scheduler",
                         "label": "Run aborted (stopped, deferred or ended early)"},
    "schedule_no_reply": {"enabled": True, "group": "Scheduler",
                          "label": "No reply to an actionable notification"},
}

#: A target: ``{"type": "all"}`` (the default) or ``{"type": "device", "ids": [...]}``.
ALL = {"type": "all"}

#: The trigger behind each event whose transition has two directions.
TRIGGER = {"aGate_online": "aGate_offline", "on_grid": "off_grid",
           "dispatch_start": "dispatch", "dispatch_end": "dispatch",
           "bridge_started": "bridge", "bridge_restarted": "bridge", "bridge_stopped": "bridge"}

#: Every trigger event and its text, worded as the Direct Connect bridge's. ``{gateway}``
#: is the gateway's name.
EVENTS = (
    Event("aGate_offline", "FranklinWH aGate",
          "aGate is UNREACHABLE [{gateway}] — check network / WiFi / power."),
    Event("aGate_online", "FranklinWH aGate", "aGate is reachable again [{gateway}]."),
    Event("off_grid", "FranklinWH off-grid",
          "Gateway went OFF-GRID [{gateway}] — running on battery. {figures}", ("figures",)),
    Event("on_grid", "FranklinWH off-grid", "Gateway is back ON-GRID [{gateway}]."),
    Event("soc_low", "FranklinWH battery",
          "Battery SoC dropped below {threshold}% [{gateway}] (now {soc}%).", ("threshold", "soc")),
    Event("soc_full", "FranklinWH battery",
          "Battery SoC reached {threshold}% [{gateway}] (now {soc}%).", ("threshold", "soc")),
    Event("dispatch_start", "FranklinWH — battery dispatch started",
          "{gateway}: Battery dispatch STARTED ({source}; {metrics}).", ("source", "metrics")),
    Event("dispatch_end", "FranklinWH — battery dispatch ended",
          "{gateway}: Battery dispatch ENDED ({detail}).", ("detail",)),
    Event("schedule_failed", "FranklinWH schedule failed",
          "Schedule '{schedule}' failed [{gateway}]: {detail}.", ("schedule", "detail")),
    Event("schedule_missed", "FranklinWH schedule missed",
          "Schedule '{schedule}' missed its window [{gateway}]: {detail}.", ("schedule", "detail")),
    Event("schedule_aborted", "FranklinWH schedule aborted",
          "Schedule '{schedule}' did not finish [{gateway}]: {detail}.", ("schedule", "detail")),
    Event("schedule_no_reply", "FranklinWH schedule: no reply",
          "Schedule '{schedule}' got no reply [{gateway}]: {detail}.", ("schedule", "detail")),
    Event("bridge_started", "FranklinWH bridge", "Bridge started (v{version}).", ("version",)),
    Event("bridge_restarted", "FranklinWH bridge",
          "Bridge restarted (v{version}{update}) after {down} down.",
          ("version", "update", "down")),
    Event("bridge_stopped", "FranklinWH bridge",
          "Bridge stopping (v{version}) — no polling or notifications until it starts again.",
          ("version",)),
    Event("trigger_test", "FranklinWH — test",
          "This is a test notification from the trigger engine."),
)

#: Below this home load (W) there is no meaningful "lasts about" figure.
MIN_LOAD_W = 50


# ── config ───────────────────────────────────────────────────


def valid_target(target: Any) -> dict:
    """The target as stored, or ValueError for one the bridge can't send to."""
    if not isinstance(target, dict) or target.get("type") not in ("all", "device"):
        raise ValueError("a target is {type: all} or {type: device, ids: [...]}")
    if target["type"] == "all":
        return dict(ALL)
    ids = target.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValueError("a device target names at least one device id")
    return {"type": "device", "ids": ids}


def merged(saved: dict | None) -> dict:
    """Stored overrides on top of the catalogue. Always well-formed."""
    cfg = {"master": True,
           "triggers": {k: {**v, "target": dict(ALL)} for k, v in DEFAULT_TRIGGERS.items()}}
    saved = saved if isinstance(saved, dict) else {}
    if isinstance(saved.get("master"), bool):
        cfg["master"] = saved["master"]
    for key, sv in (saved.get("triggers") or {}).items():
        t = cfg["triggers"].get(key)
        if t is None or not isinstance(sv, dict):
            continue
        if isinstance(sv.get("enabled"), bool):
            t["enabled"] = sv["enabled"]
        if "threshold" in t and isinstance(sv.get("threshold"), (int, float)) \
                and not isinstance(sv.get("threshold"), bool):
            t["threshold"] = max(0, min(100, sv["threshold"]))
        if "target" in sv:
            with contextlib.suppress(ValueError):
                t["target"] = valid_target(sv["target"])
    return cfg


def apply(cfg: dict, patch: dict) -> dict:
    """``cfg`` with a PUT's changes. Raises ValueError for a bad target or threshold, so a
    save that can't be kept is refused, not half done."""
    out = json.loads(json.dumps(cfg))
    if "master" in patch:
        if not isinstance(patch["master"], bool):
            raise ValueError("master is true or false")
        out["master"] = patch["master"]
    for key, sv in (patch.get("triggers") or {}).items():
        t = out["triggers"].get(key)
        if t is None:
            raise ValueError(f"unknown trigger {key!r}")
        if not isinstance(sv, dict):
            raise ValueError(f"{key}: expected an object")
        if "enabled" in sv:
            if not isinstance(sv["enabled"], bool):
                raise ValueError(f"{key}: enabled is true or false")
            t["enabled"] = sv["enabled"]
        if "threshold" in sv:
            v = sv["threshold"]
            if "threshold" not in t or isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not 0 <= v <= 100:
                raise ValueError(f"{key}: threshold is 0 to 100")
            t["threshold"] = v
        if "target" in sv:
            t["target"] = valid_target(sv["target"])
    return out


def stored(cfg: dict) -> dict:
    """What is kept: master, and per trigger enabled, threshold and target."""
    return {"master": cfg["master"],
            "triggers": {k: {kk: vv for kk, vv in v.items()
                             if kk in ("enabled", "threshold", "target")}
                         for k, v in cfg["triggers"].items()}}


def enabled(cfg: dict, key: str) -> bool:
    return bool(cfg.get("master", True)) and bool(cfg["triggers"].get(key, {}).get("enabled"))


# ── evaluation ───────────────────────────────────────────────


def is_off_grid(points: dict) -> bool | None:
    """Off-grid from ``connection_state`` (the aggregator's rule), None when not read."""
    cs = points.get("connection_state")
    if cs is None:
        return None
    return "disconnect" in str(cs).lower()


def duration(seconds: float) -> str:
    """``5 h 40 min``, ``25 min``, ``40 s``."""
    s = max(0, round(seconds))
    h, m = divmod(s // 60, 60)
    if h:
        return f"{h} h {m} min"
    return f"{m} min" if m else f"{s} s"


def off_grid_figures(points: dict) -> str:
    """The battery figures for an off-grid notification: SoC, energy left, the home load
    and about how long the battery lasts at that load. Only what was read is shown."""
    soc, wh, load = points.get("soc"), points.get("wh_available"), points.get("home_load_ext")
    parts = []
    if isinstance(soc, (int, float)):
        left = f", {wh / 1000:.1f} kWh left" if isinstance(wh, (int, float)) else ""
        parts.append(f"Battery {round(soc)}%{left}")
    if isinstance(load, (int, float)):
        parts.append(f"home using {load / 1000:.1f} kW")
    text = "; ".join(parts)
    if isinstance(wh, (int, float)) and isinstance(load, (int, float)) and load >= MIN_LOAD_W:
        text += f": lasts about {duration(wh / load * 3600)} at current usage"
    return f"{text[:1].upper()}{text[1:]}." if text else ""


def evaluate(prev: dict | None, cur: dict, cfg: dict) -> list[tuple[str, dict]]:
    """One gateway's transitions worth notifying, gated by config: ``[(event, values)]``.
    ``prev`` None (first poll after start) gives none, so a restart doesn't re-announce."""
    if prev is None or not cfg.get("master", True):
        return []
    out: list[tuple[str, dict]] = []
    was, now = is_off_grid(prev), is_off_grid(cur)
    if enabled(cfg, "off_grid") and was is not None and now is not None and was != now:
        out.append(("off_grid", {"figures": off_grid_figures(cur)}) if now else ("on_grid", {}))
    ps, cs = prev.get("soc"), cur.get("soc")
    if isinstance(ps, (int, float)) and isinstance(cs, (int, float)):
        if enabled(cfg, "soc_low"):
            thr = cfg["triggers"]["soc_low"]["threshold"]
            if ps >= thr > cs:
                out.append(("soc_low", {"threshold": thr, "soc": round(cs)}))
        if enabled(cfg, "soc_full"):
            thr = cfg["triggers"]["soc_full"]["threshold"]
            if ps < thr <= cs:
                out.append(("soc_full", {"threshold": thr, "soc": round(cs)}))
    return out


def stats_by_trigger(rows: list[tuple[str, int, float | None]]) -> dict[str, dict]:
    """``{trigger: {count, last_ts}}`` from per-event ``(event, count, last_ts)``, so both
    directions of a trigger count."""
    out: dict[str, dict] = {}
    for event, count, last in rows:
        key = TRIGGER.get(event, event)
        t = out.setdefault(key, {"count": 0, "last_ts": None})
        t["count"] += count
        t["last_ts"] = max(t["last_ts"] or 0, last or 0) or None
    return out


# ── the engine ───────────────────────────────────────────────


class Triggers:
    """Holds the config and each gateway's previous points; fires through the Notifier."""

    def __init__(self, db: aiosqlite.Connection, actionable: Actionable) -> None:
        self._db = db
        self._act = actionable
        self._prev: dict[str, dict] = {}
        self.cfg = merged(None)

    async def load(self) -> dict:
        raw = await get_app_config(self._db, CONFIG_KEY, "")
        try:
            self.cfg = merged(json.loads(raw) if raw else None)
        except ValueError:
            self.cfg = merged(None)
        return self.cfg

    async def save(self, patch: dict) -> dict:
        cfg = apply(self.cfg, patch)
        await set_app_config(self._db, CONFIG_KEY, json.dumps(stored(cfg)))
        self.cfg = cfg
        return cfg

    async def _device_ids(self, key: str) -> list[str] | None:
        """None for all enabled devices, else the enabled ones among those chosen (a
        switched-off device stays off for its triggers too)."""
        target = self.cfg["triggers"].get(key, {}).get("target") or ALL
        if target["type"] != "device":
            return None
        on = {d["id"] for d in await get_notify_devices(self._db) if d["enabled"]}
        return [i for i in target["ids"] if i in on]

    async def fire(self, event: str, *, gateway: str | None = None, **values) -> int:
        """Send ``event`` if its trigger is on. Logged in the app log either way it is
        sent. Never raises. Returns the number of devices that accepted it."""
        key = TRIGGER.get(event, event)
        if key in self.cfg["triggers"] and not enabled(self.cfg, key):
            return 0
        from franklinwh_bridge.actionable import _TEMPLATES

        _, message = _TEMPLATES.render(event, gateway=gateway or "", **values)
        (logger.info if event == "trigger_test" else logger.warning)(
            "notify [%s]: %s", event, message)
        notifier = self._act.notifier
        if notifier is None:
            return 0
        try:
            ids = await self._device_ids(key)
            targets = await self._act.targets(ids) if ids is not None else \
                await self._act.targets()
            if not targets:
                await notifier.ledger.arecord("SKIPPED", event, gateway=gateway,
                                              message=message, status="no device to send to")
                return 0
            res = await notifier.notify(event, targets, gateway=gateway, **values)
        except Exception as exc:  # a notification never breaks its caller
            logger.warning("notify [%s] failed: %s", event, exc)
            return 0
        return res.delivered

    async def on_points(self, gateway_id: str, name: str, points: dict) -> None:
        """A gateway's new poll: fire its off-grid and SoC transitions. Points missing a
        value keep the previous one, so a partial read is not a transition."""
        cur = {**self._prev.get(gateway_id, {}),
               **{k: points[k] for k in ("connection_state", "soc", "wh_available",
                                         "home_load_ext") if points.get(k) is not None}}
        prev = self._prev.get(gateway_id)
        self._prev[gateway_id] = cur
        for event, values in evaluate(prev, cur, self.cfg):
            await self.fire(event, gateway=name, **values)

    def forget(self, gateway_id: str) -> None:
        self._prev.pop(gateway_id, None)

    # ── bridge start and stop ──
    async def bridge_up(self, version: str, now: float | None = None) -> None:
        """Restarted after a clean stop (how long it was down, and the version it came
        from if that changed), else started. The stop record is used once. Never raises:
        it runs as a background task at startup."""
        try:
            raw = await get_app_config(self._db, STOP_KEY, "")
            await set_app_config(self._db, STOP_KEY, "")
        except Exception as exc:  # noqa: BLE001 — a start notice must not fail the bridge
            logger.warning("notify [bridge start]: could not read the stop record: %s", exc)
            return
        try:
            stop = json.loads(raw) if raw else None
        except ValueError:
            stop = None
        if not isinstance(stop, dict) or not isinstance(stop.get("ts"), (int, float)):
            await self.fire("bridge_started", version=version)
            return
        old = stop.get("version")
        await self.fire("bridge_restarted", version=version,
                        down=duration((time.time() if now is None else now) - stop["ts"]),
                        update=f", updated from {old}" if old and old != version else "")

    async def bridge_down(self, version: str) -> None:
        """Record a clean stop, then say so."""
        await set_app_config(self._db, STOP_KEY,
                             json.dumps({"ts": time.time(), "version": version}))
        await self.fire("bridge_stopped", version=version)

    # ── stats ──
    async def stats(self) -> dict[str, dict]:
        """Per trigger: how many were sent and when last (the ledger keeps 30 days)."""
        events = [e.key for e in EVENTS]
        q = ",".join("?" * len(events))
        async with self._db.execute(
                f"SELECT event, COUNT(*), MAX(ts) FROM notify_ledger WHERE direction IN "
                f"('SENT','ERROR') AND event IN ({q}) GROUP BY event", events) as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        return stats_by_trigger(rows)
