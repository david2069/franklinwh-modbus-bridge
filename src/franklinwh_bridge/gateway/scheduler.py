"""Schedule engine (SCH1) — declarative time → command-handler dispatch.

A thin, deterministic loop that, each tick, computes the *winning* active
schedule entry per target and drives the existing ``CommandHandler`` to match.
It is **not** a HEMS — see ``docs/scheduling-and-orchestration-design.md``.

Design points realised here:
- **VPP overrides native TOU** (§4a): dispatch is just the normal command path,
  so taking control needs no mode switch. The lever is the **release/gap**
  behaviour on window exit: ``release`` hands back to native TOU, ``hold`` keeps
  a 0 W VPP standby to keep native suppressed.
- **Conflict policy** (§4b): when a target is already under *foreign* (manual)
  control, ``defer``/``wait`` leave it alone, ``override`` preempts it.
- **Idempotent**: re-issuing the same desired state is a no-op; we dispatch only
  on a state change, so the loop never thrashes the battery.

The control-bearing logic lives in module-level pure functions
(``entry_active_at``, ``winner``, ``action_to_commands`` …) so it is unit-tested
without a controller, a registry, or hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Resolver: (target_type, target_id) -> list of command-handler-likes. Each must
# expose ``handle_command(slug, value)`` (awaitable) and ``state`` with
# ``.active`` / ``.action``. For SCH1 this resolves a single gateway; service/
# site fan-out (multiple handlers) is SCH3/MP5 and already fits this shape.
Resolver = Callable[[str, str | None], list[Any]]

DEFAULT_TICK_S = 15  # window resolution is per-minute; 15s keeps latency low

# action → the CommandHandler's canonical state label (for ownership/idempotency)
_DISPLAY = {
    "force_charge": "Force Charge",
    "force_discharge": "Force Discharge",
    "force_standby": "Force Standby",
    "release": "",
}
# actions that hold sustained control (and thus get a release on window exit)
_SUSTAINED = frozenset({"force_charge", "force_discharge", "force_standby"})


# ── pure helpers (unit-tested) ────────────────────────────────


def _to_minutes(hhmm: str) -> int:
    """'HH:MM' → minutes since midnight. Tolerates 'H:MM' and bad input → 0."""
    try:
        h, m = hhmm.strip().split(":")
        return (int(h) % 24) * 60 + int(m) % 60
    except (ValueError, AttributeError):
        return 0


def parse_when(when_spec: dict) -> tuple[set[int], list[tuple[int, int]]]:
    """Return (weekday-set, [(start_min, end_min)…]) from a when-spec.

    days: ints 0..6, Monday=0 (Python ``date.weekday``). Empty/absent = every
    day. windows: list of {start,end} 'HH:MM'. end<=start means the window wraps
    past midnight.
    """
    days = {int(d) for d in (when_spec.get("days") or []) if 0 <= int(d) <= 6}
    windows: list[tuple[int, int]] = []
    for w in when_spec.get("windows") or []:
        windows.append((_to_minutes(w.get("start", "00:00")), _to_minutes(w.get("end", "24:00"))))
    return days, windows


def _window_contains(start: int, end: int, t: int) -> bool:
    """Is minute ``t`` inside [start, end)? Handles past-midnight wrap."""
    if end == start:
        return False  # zero-length window matches nothing
    if start < end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight


def entry_active_at(when_spec: dict, now: datetime) -> bool:
    """True if ``now`` falls inside any of the entry's windows on a matching day."""
    days, windows = parse_when(when_spec)
    if days and now.weekday() not in days:
        return False
    t = now.hour * 60 + now.minute
    return any(_window_contains(s, e, t) for s, e in windows)


def next_fire(when_spec: dict, now: datetime, horizon_days: int = 8) -> datetime | None:
    """Next window *start* at/after ``now`` (minute resolution), or None."""
    days, windows = parse_when(when_spec)
    if not windows:
        return None
    now = now.replace(second=0, microsecond=0)
    for day_offset in range(horizon_days):
        day = now + timedelta(days=day_offset)
        if days and day.weekday() not in days:
            continue
        for start_min, _end in sorted(windows):
            cand = day.replace(hour=0, minute=0) + timedelta(minutes=start_min)
            if cand >= now:
                return cand
    return None


def winner(entries: list[dict], now: datetime) -> dict | None:
    """Pick the single winning *active* entry for one target.

    Entries are pre-sorted (priority desc, created_at desc) by the store, so the
    first active one is the winner: highest priority, newest as tie-break
    (last-writer). Disabled entries are excluded by the caller.
    """
    for e in entries:
        if entry_active_at(e.get("when_spec", {}), now):
            return e
    return None


def action_signature(action: str, params: dict) -> str:
    """Stable string identifying a desired dispatch, for change detection."""
    p = params or {}
    return "|".join(
        str(x) for x in (
            action,
            p.get("power_w", ""), p.get("power_pct", ""),
            p.get("pct", ""), p.get("mode", ""),
            p.get("duration_s", ""), p.get("target_soc", ""),
        )
    )


def action_to_commands(action: str, params: dict) -> list[tuple[str, str]]:
    """Translate (action, params) into ordered (slug, value) command-handler calls.

    Power/duration/target-SoC are set *before* the battery command so the
    handler applies them. Returns [] for an unknown action.
    """
    p = params or {}
    pre: list[tuple[str, str]] = []
    if "duration_s" in p:
        pre.append(("battery_command_duration", str(int(p["duration_s"]))))
    if "target_soc" in p:
        pre.append(("battery_command_target_soc", str(int(p["target_soc"]))))

    if action in ("force_charge", "force_discharge"):
        if p.get("power_pct"):
            pre.append(("battery_command_power_pct", str(int(p["power_pct"]))))
        elif p.get("power_w"):
            pre.append(("battery_command_power", str(int(p["power_w"]))))
        label = _DISPLAY[action]
        return [*pre, ("battery_command", label)]
    if action == "force_standby":
        return [*pre, ("battery_command", "Force Standby")]
    if action == "release":
        return [("battery_command", "Release")]
    if action == "reserve_self":
        return [("self_reserve_pct", str(int(p.get("pct", 0))))]
    if action == "reserve_tou":
        return [("tou_reserve_pct", str(int(p.get("pct", 0))))]
    if action == "mode":
        return [("operating_mode", str(p.get("mode", "")))]
    return []


# ── the engine ────────────────────────────────────────────────


class ScheduleEngine:
    """Evaluates schedule entries and drives command handlers."""

    def __init__(
        self,
        db: Any,
        resolver: Resolver,
        *,
        now_fn: Callable[[], datetime] | None = None,
        tick_s: int = DEFAULT_TICK_S,
        on_audit: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._resolver = resolver
        self._now = now_fn or datetime.now
        self._tick_s = tick_s
        self._on_audit = on_audit
        self._entries: list[dict] = []
        self._task: asyncio.Task | None = None
        # per-target ownership of the dispatch we placed:
        #   target_key -> {entry_id, signature, action, display, release, mode}
        self._owned: dict[str, dict] = {}
        # targets we're currently deferring on (so we audit the defer once, not
        # every tick while a manual command stays active)
        self._deferred: set[str] = set()

    # ---- lifecycle ----

    async def load(self) -> None:
        """(Re)load enabled entries from the store, store-sorted."""
        from franklinwh_bridge.store.db import get_schedules

        rows = await get_schedules(self._db)
        self._entries = [r for r in rows if r.get("enabled")]

    async def reload(self) -> None:
        """Reload + evaluate immediately (call after a CRUD change)."""
        await self.load()
        await self.tick()

    async def start(self) -> None:
        await self.load()
        self._task = asyncio.create_task(self._loop())
        logger.info("Schedule engine started (%d active entries)", len(self._entries))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception as exc:  # never let one bad tick kill the loop
                    logger.warning("Schedule tick failed: %s", exc)
                await asyncio.sleep(self._tick_s)
        except asyncio.CancelledError:
            pass

    # ---- evaluation ----

    def _targets(self) -> dict[tuple[str, str | None], list[dict]]:
        """Group active entries by (target_type, target_id)."""
        groups: dict[tuple[str, str | None], list[dict]] = {}
        for e in self._entries:
            key = (e.get("target_type", "gateway"), e.get("target_id"))
            groups.setdefault(key, []).append(e)
        return groups

    async def tick(self, now: datetime | None = None) -> None:
        """Evaluate every target once and reconcile dispatch to the winner."""
        now = now or self._now()
        # Keep ownership for targets that still have entries; targets dropped
        # entirely (entry deleted) are reconciled to "no desired" below.
        seen: set[str] = set()
        for (ttype, tid), entries in self._targets().items():
            win = winner(entries, now)
            handlers = self._resolver(ttype, tid)
            for idx, h in enumerate(handlers):
                tkey = f"{ttype}:{tid}:{idx}"
                seen.add(tkey)
                await self._reconcile(tkey, h, win)

        # A target we used to own but whose entries are now gone → release it.
        for tkey in list(self._owned):
            if tkey not in seen:
                ttype, tid, idx = tkey.split(":", 2)
                handlers = self._resolver(ttype, tid or None)
                if int(idx) < len(handlers):
                    await self._reconcile(tkey, handlers[int(idx)], None)
                else:
                    self._owned.pop(tkey, None)

    async def _reconcile(self, tkey: str, handler: Any, win: dict | None) -> None:
        own = self._owned.get(tkey)

        # ── no winning entry: release/hold if we hold a sustained dispatch ──
        if win is None:
            if own and own["action"] in _SUSTAINED:
                if own["release"] == "hold" and own.get("mode") != "hold":
                    await self._dispatch(handler, "force_standby", {}, label="hold")
                    self._owned[tkey] = {**own, "action": "force_standby",
                                         "display": "Force Standby", "mode": "hold"}
                    await self._audit(own.get("entry_id"), "hold", tkey,
                                      "ok", "window exit — holding native suppressed")
                elif own["release"] != "hold":
                    await self._send(handler, [("battery_command", "Release")])
                    self._owned.pop(tkey, None)
                    await self._audit(own.get("entry_id"), "release", tkey,
                                      "ok", "window exit — released to native")
            else:
                self._owned.pop(tkey, None)
            return

        action = win["action"]
        params = win.get("params", {})
        sig = action_signature(action, params)
        display = _DISPLAY.get(action)

        # ── foreign (manual) control present? ──
        we_own_current = bool(
            own and handler.state.active and handler.state.action == own.get("display")
        )
        foreign = handler.state.active and not we_own_current
        if foreign and action in _SUSTAINED:
            policy = win.get("conflict", "defer")
            if policy in ("defer", "wait"):
                # leave the manual command alone; drop any stale ownership
                if own:
                    self._owned.pop(tkey, None)
                if tkey not in self._deferred:  # audit the defer once, not per tick
                    self._deferred.add(tkey)
                    await self._audit(win["id"], action, tkey, "deferred",
                                      f"target under manual control ({handler.state.action})")
                return
            # override → fall through and take control (logs a supersede via handler)
        self._deferred.discard(tkey)  # no longer deferring this target

        # ── idempotent: already applying this exact desired state? ──
        if own and own["signature"] == sig and (
            action not in _SUSTAINED or handler.state.action == display
        ):
            return

        await self._dispatch(handler, action, params, label=win.get("name", action))
        self._owned[tkey] = {
            "entry_id": win["id"], "signature": sig, "action": action,
            "display": display, "release": win.get("release", "release"),
            "mode": "window",
        }
        await self._audit(win["id"], action, tkey, "ok",
                          f"dispatched {win.get('name', action)}")

    async def _dispatch(self, handler: Any, action: str, params: dict, label: str) -> None:
        cmds = action_to_commands(action, params)
        if not cmds:
            logger.warning("Schedule: unknown action %r — skipped", action)
            return
        logger.info("Schedule dispatch: %s (%s)", label, action)
        await self._send(handler, cmds)

    async def _send(self, handler: Any, cmds: list[tuple[str, str]]) -> None:
        for slug, value in cmds:
            await handler.handle_command(slug, value)

    async def _audit(
        self, schedule_id: str | None, action: str, target: str,
        result: str, detail: str,
    ) -> None:
        if self._on_audit is None:
            return
        try:
            await self._on_audit(schedule_id, action, target, result, detail)
        except Exception as exc:
            logger.debug("Schedule audit failed: %s", exc)
