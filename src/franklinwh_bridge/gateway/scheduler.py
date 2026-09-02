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
from datetime import date, datetime, timedelta
from typing import Any

from franklinwh_bridge.gateway.scheduler_conditions import evaluate as eval_conditions
from franklinwh_bridge.gateway.scheduler_sensors import snapshot as sensor_snapshot
from franklinwh_bridge.gateway.scheduler_triggers import prev_fire_at

logger = logging.getLogger(__name__)

# Resolver: (target_type, target_id) -> list of (gateway_id, handler) pairs.
# Each handler exposes ``handle_command(slug, value)`` (awaitable) and ``state``
# with ``.active`` / ``.action``. A ``gateway`` target yields one pair; a
# ``service``/``site`` target fans out to every member gateway (SCH3). The
# gateway_id makes ownership stable across a changing member set.
Resolver = Callable[[str, str | None], list[tuple[str, Any]]]

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
    day (ignored entirely when ``date`` is set — see ``entry_date``). windows:
    list of {start,end} 'HH:MM'. end<=start means the window wraps past
    midnight.
    """
    days = {int(d) for d in (when_spec.get("days") or []) if 0 <= int(d) <= 6}
    windows: list[tuple[int, int]] = []
    for w in when_spec.get("windows") or []:
        windows.append((_to_minutes(w.get("start", "00:00")), _to_minutes(w.get("end", "24:00"))))
    return days, windows


def entry_date(when_spec: dict) -> date | None:
    """Parse when_spec['date'] (ISO 'YYYY-MM-DD'), or None for a recurring entry.

    A one-time entry is scoped to this exact calendar date instead of a
    weekday set — it fires only that day and goes dormant forever after,
    with no separate "consumed" flag needed.
    """
    date_str = when_spec.get("date")
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None


def _window_contains(start: int, end: int, t: int) -> bool:
    """Is minute ``t`` inside [start, end)? Handles past-midnight wrap."""
    if end == start:
        return False  # zero-length window matches nothing
    if start < end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight


def entry_active_at(when_spec: dict, now: datetime) -> bool:
    """True if ``now`` falls inside any of the entry's windows on a matching day."""
    d = entry_date(when_spec)
    if d is not None and now.date() != d:
        return False
    days, windows = parse_when(when_spec)
    if d is None and days and now.weekday() not in days:
        return False
    t = now.hour * 60 + now.minute
    return any(_window_contains(s, e, t) for s, e in windows)


def next_fire(when_spec: dict, now: datetime, horizon_days: int = 8) -> datetime | None:
    """Next window *start* at/after ``now`` (minute resolution), or None.

    A dated (one-time) entry is checked against its own date directly, not
    the rolling ``horizon_days`` window, since it may be scheduled further
    out than a recurring entry ever needs to look.
    """
    days, windows = parse_when(when_spec)
    if not windows:
        return None
    now = now.replace(second=0, microsecond=0)
    d = entry_date(when_spec)
    if d is not None:
        if d < now.date():
            return None  # date has passed — this entry will never fire again
        for start_min, _end in sorted(windows):
            cand = datetime(d.year, d.month, d.day) + timedelta(minutes=start_min)
            if cand >= now:
                return cand
        return None
    for day_offset in range(horizon_days):
        day = now + timedelta(days=day_offset)
        if days and day.weekday() not in days:
            continue
        for start_min, _end in sorted(windows):
            cand = day.replace(hour=0, minute=0) + timedelta(minutes=start_min)
            if cand >= now:
                return cand
    return None


def _is_v2(entry: dict) -> bool:
    """A v2 entry carries a trigger kind and/or a condition tree. Only these opt
    into fire-based activation and release_policy; legacy entries are untouched."""
    return bool(
        entry.get("trigger_kind") or entry.get("entry_conditions") or entry.get("exit_conditions")
    )


def trigger_active_at(entry: dict, now: datetime) -> bool:
    """Is a fire-based (v2) entry currently active?

    Active during ``[prev_fire, prev_fire + duration_s)``. ``always`` is
    continuously active (its entry-conditions gate decides dispatch). A missing
    ``duration_s`` yields a minimal one-minute active window — enough for an
    instantaneous fire (e.g. a mode/reserve set); sustained actions should set a
    duration or an exit condition.
    """
    kind = entry.get("trigger_kind")
    if kind == "always":
        return True
    spec = dict(entry.get("trigger_spec") or {})
    spec["kind"] = kind
    prev = prev_fire_at(spec, now)
    if prev is None:
        return False
    dur = entry.get("duration_s")
    window_s = int(dur) if dur else 60
    return now < prev + timedelta(seconds=window_s)


def entry_active(entry: dict, now: datetime) -> bool:
    """Unified activation: fire-based for v2 trigger entries, window-based for
    legacy (and any v2 entry that still expresses timing via when_spec)."""
    if entry.get("trigger_kind"):
        return trigger_active_at(entry, now)
    return entry_active_at(entry.get("when_spec", {}), now)


def _missed_fire_time(entry: dict, since: datetime, now: datetime) -> datetime | None:
    """The most recent fire/window-start in ``(since, now]`` for an entry, or None.

    Used by catch-up to decide whether an outage swallowed a fire. The caller
    only invokes this for entries that are NOT currently active (a still-open
    window is resumed by the normal tick, not treated as missed). ``always`` has
    no discrete fire to miss.
    """
    kind = entry.get("trigger_kind")
    if kind:
        if kind == "always":
            return None
        spec = dict(entry.get("trigger_spec") or {})
        spec["kind"] = kind
        last = prev_fire_at(spec, now)
        return last if (last is not None and last >= since) else None
    nf = next_fire(entry.get("when_spec", {}), since)
    return nf if (nf is not None and nf <= now) else None


def winner(entries: list[dict], now: datetime) -> dict | None:
    """Pick the single winning *active* entry for one target, ignoring gates.

    Entries are pre-sorted (priority desc, created_at ASC) by the store, so the
    first active one is the winner: highest priority, oldest as tie-break — an
    entry added later never displaces an established one at equal priority.
    Disabled entries are excluded by the caller.

    The engine itself uses :meth:`SchedulerEngine._battery_winner`, which also
    skips entries whose conditions currently fail; this stays as the pure
    activity-only ranking.
    """
    for e in entries:
        if entry_active(e, now):
            return e
    return None


def action_signature(action: str, params: dict) -> str:
    """Stable string identifying a desired dispatch, for change detection."""
    p = params or {}
    return "|".join(
        str(x)
        for x in (
            action,
            p.get("power_w", ""),
            p.get("power_pct", ""),
            p.get("pct", ""),
            p.get("mode", ""),
            p.get("duration_s", ""),
            p.get("target_soc", ""),
        )
    )


def _leaf_str(t: dict) -> str:
    """Readable `sensor op value (live=…)` for one condition-trace leaf."""
    val = t.get("value")
    if t.get("op") == "between" and t.get("value2") is not None:
        val = f"{t.get('value')}..{t.get('value2')}"
    return f"{t.get('sensor')} {t.get('op')} {val} (live={t.get('live_value')})"


def _condition_reason(prefix: str, trace: list[dict]) -> str:
    """One-line audit detail listing the FAILING conditions (with live values)."""
    fails = [_leaf_str(t) for t in trace if not t.get("result")]
    return f"{prefix}: " + "; ".join(fails[:4]) if fails else prefix


def _action_summary(action: str, params: dict) -> str:
    """Human one-liner for an action + its resolved power/pct, e.g.
    ``Force Discharge @ 100%`` / ``Force Charge @ 1000W`` / ``Self-Reserve 20%``."""
    labels = {
        "force_charge": "Force Charge",
        "force_discharge": "Force Discharge",
        "force_standby": "Force Standby",
        "release": "Release",
        "reserve_self": "Self-Consumption Reserve",
        "reserve_tou": "TOU Reserve",
        "mode": "Operating Mode",
        "none": "No battery action",
    }
    lbl = labels.get(action, action)
    p = params or {}
    if action in ("force_charge", "force_discharge"):
        if p.get("power_pct"):
            return f"{lbl} @ {p['power_pct']}%"
        if p.get("power_w"):
            return f"{lbl} @ {p['power_w']}W"
        return lbl
    if action in ("reserve_self", "reserve_tou") and "pct" in p:
        return f"{lbl} {p['pct']}%"
    if action == "mode" and p.get("mode"):
        return f"{lbl}: {p['mode']}"
    return lbl


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
        points_fn: Callable[[str], dict] | None = None,
        connectivity: Any | None = None,
        ha_action_fn: Callable[[str, str, str, dict], Awaitable[dict]] | None = None,
        gw_label_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._db = db
        self._resolver = resolver
        # Optional gw_id -> human label ("Default Gateway", "FHP 2 (mock)") for
        # readable audit targets. None → the raw gateway id is shown.
        self._gw_label_fn = gw_label_fn
        # Optional HA-action dispatcher: (instance_id, entity_id, service, data)
        # -> {"ok": bool, ...}. Wired to HaRegistry.call_service in main.py. None
        # → HA actions are skipped (no HA configured).
        self._ha_action_fn = ha_action_fn
        self._now = now_fn or datetime.now
        self._tick_s = tick_s
        self._on_audit = on_audit
        # Optional ConnectivityMonitor — driven once per tick to detect outages
        # (staleness) alongside its SampleBus-driven recovery.
        self._connectivity = connectivity
        # gw_id -> latest cached poll points (no Modbus call). Feeds the sensor
        # snapshot that entry/exit condition trees evaluate against. None → the
        # engine runs condition-free (legacy SCH1 behaviour, all sensors None).
        self._points_fn = points_fn
        self._entries: list[dict] = []
        self._task: asyncio.Task | None = None
        # serialise tick() so reload()'s immediate eval can't race the periodic
        # loop tick (both would dispatch before ownership is recorded — observed
        # as a double command on hardware during live LT-1 testing).
        self._tick_lock = asyncio.Lock()
        # Ownership is keyed per *physical gateway* so a fan-out target
        # (service/site) tracks each member independently and a changing member
        # set can't shift ownership between gateways. Key = (ttype, tid, gw_id).
        #   tkey -> {entry_id, signature, action, display, release, mode}
        self._owned: dict[tuple, dict] = {}
        # targets we're currently deferring on (so we audit the defer once, not
        # every tick while a manual command stays active)
        self._deferred: set[tuple] = set()
        # (tkey, loser_id, winner_id) already audited as skipped, and the last
        # battery-lane winner per target — so losing the gateway is logged once
        # per contest rather than every tick.
        self._skipped: set[tuple] = set()
        self._last_win: dict[tuple, str] = {}
        # tkey -> entry_id whose CURRENT window we've already completed (its
        # sustained dispatch ended in-window via the watchdog or an external
        # release). Suppresses re-firing until the window is re-entered.
        self._expired: dict[tuple, str] = {}
        # targets whose entry-conditions gate is currently failing (so we audit
        # the gate once, not every tick while the window stays open and gated).
        self._gated: set[tuple] = set()
        # per-tick gw_id -> sensor snapshot, rebuilt each _tick_locked so one
        # gateway's points are read at most once per tick and every tree sees a
        # consistent view.
        self._snap_cache: dict[str, dict] = {}
        # Condition-dwell timers: tkey -> (entry_id, first_eligible_at). An entry
        # with entry_hold_s > 0 only fires once it's been eligible (window active
        # + entry conditions passing) continuously for that long. Reset when the
        # gate fails or the window exits. In-memory: on restart the timer restarts
        # (the safe direction — never fires early). _dwelling audits the wait once.
        self._dwell_since: dict[tuple, tuple[str, datetime]] = {}
        self._dwelling: set[tuple] = set()
        # One-shot HA actions are edge-triggered: tkey -> entry_id we've already
        # run actions for this activation. Cleared on window exit so re-entry
        # re-fires. Prevents re-running on a battery re-dispatch (sig change).
        self._ha_fired: dict[tuple, str] = {}
        # tkey -> the entry that fired, kept so its exit-tagged HA actions can run
        # when the activation ends (window/trigger inactive, exit condition, etc.).
        self._exit_entry: dict[tuple, dict] = {}

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

    async def catchup(
        self, gateway_id: str, since_ts: float, now: datetime | None = None
    ) -> dict:
        """After an outage on ``gateway_id`` spanning ``since_ts``..now, resolve the
        fires that were swallowed per each entry's ``missed_policy``.

        - A window still open now is left for the normal tick to resume (not
          "missed") — this is the default ``late_fire_remaining`` behaviour.
        - A window that fully passed during the outage is recorded as ``missed``.
        - ``late_fire_always``: additionally **re-dispatch** the entry now for its
          duration (``catchup_duration_s`` if set, else ``duration_s``), gated by
          ``entry_conditions`` — audited ``late_fired``. The dispatch carries a
          command watchdog so the handler auto-releases (it takes no engine
          ownership; the stateless tick won't manage or release it).

        Returns ``{"missed": [...], "late_fired": [...]}`` (entry ids) for linking
        to the OutageEvent. Serialised against the tick.

        Still a follow-up: startup catch-up (needs a persisted last-ok timestamp
        across restarts — this runs on live recovery only).
        """
        now_dt = now or self._now()
        since_dt = datetime.fromtimestamp(since_ts)
        missed_ids: list[str] = []
        late_fired: list[str] = []
        async with self._tick_lock:
            self._snap_cache = {}  # fresh reads for any late-fire gate check
            for entry in self._entries:
                pairs = self._resolver(entry.get("target_type", "gateway"), entry.get("target_id"))
                handlers = {gw: h for gw, h in pairs}
                if gateway_id not in handlers:
                    continue
                if entry_active(entry, now_dt):
                    continue  # still-open window → the normal tick resumes it
                missed_at = _missed_fire_time(entry, since_dt, now_dt)
                if missed_at is None:
                    continue
                missed_ids.append(entry["id"])
                policy = entry.get("missed_policy") or "late_fire_remaining"
                ttype = entry.get("target_type", "gateway")
                target = f"{ttype}:{entry.get('target_id') or ''}:{gateway_id}"

                if policy != "late_fire_always":
                    await self._audit(
                        entry["id"], entry.get("action"), target, "missed",
                        f"fire at {missed_at.isoformat()} missed during outage (policy={policy})",
                    )
                    continue

                # late_fire_always → re-dispatch now, gated by entry_conditions.
                entry_tree = entry.get("entry_conditions")
                if entry_tree is not None:
                    ok, trace = eval_conditions(entry_tree, self._snapshot(gateway_id, now_dt))
                    if not ok:
                        await self._audit(
                            entry["id"], entry.get("action"), target, "missed",
                            _condition_reason(
                                f"missed at {missed_at.isoformat()} — late_fire_always gated", trace
                            ),
                        )
                        continue
                params = dict(entry.get("params") or {})
                dur = entry.get("catchup_duration_s") or entry.get("duration_s")
                if dur:
                    params.setdefault("duration_s", dur)
                cmds = action_to_commands(entry.get("action"), params)
                if cmds:
                    await self._send(handlers[gateway_id], cmds)
                    late_fired.append(entry["id"])
                    await self._audit(
                        entry["id"], entry.get("action"), target, "late_fired",
                        f"late-fired (missed at {missed_at.isoformat()}, policy late_fire_always)",
                    )
        if missed_ids:
            logger.info(
                "Catch-up: gateway %s — %d missed, %d late-fired after recovery",
                gateway_id, len(missed_ids), len(late_fired),
            )
        return {"missed": missed_ids, "late_fired": late_fired}

    async def execute(
        self, schedule_id: str, *, force: bool = False, now: datetime | None = None
    ) -> dict:
        """Fire an entry's action immediately (manual "fire now"), regardless of
        its trigger/window. Respects ``entry_conditions`` unless ``force``.

        Dispatches with the entry's ``duration_s`` as the command watchdog so the
        handler auto-releases — this is a deliberate override that does NOT take
        engine ownership (the periodic tick won't manage or release it).
        Serialised against the tick so the snapshot can't race a reconcile.
        """
        from franklinwh_bridge.store.db import get_schedule

        if self._db is None:
            return {"status": "no_store", "schedule_id": schedule_id}
        entry = await get_schedule(self._db, schedule_id)
        if entry is None:
            return {"status": "not_found", "schedule_id": schedule_id}

        action = entry.get("action")
        params = dict(entry.get("params") or {})
        if entry.get("duration_s"):
            params.setdefault("duration_s", entry["duration_s"])
        ttype = entry.get("target_type", "gateway")
        tid = entry.get("target_id")

        async with self._tick_lock:
            self._snap_cache = {}  # fresh reads for this manual fire
            now_dt = now or self._now()
            pairs = self._resolver(ttype, tid)
            if not pairs:
                return {"status": "no_target", "schedule_id": schedule_id}

            dispatched: list[str] = []
            gated: list[str] = []
            entry_tree = entry.get("entry_conditions")
            for gw_id, handler in pairs:
                target = (ttype, tid, gw_id)
                if not force and entry_tree is not None:
                    ok, trace = eval_conditions(entry_tree, self._snapshot(gw_id, now_dt))
                    if not ok:
                        gated.append(gw_id)
                        await self._audit(
                            schedule_id, action, target, "gated",
                            _condition_reason("execute gated", trace),
                        )
                        continue
                cmds = action_to_commands(action, params)
                if not cmds:
                    return {"status": "unknown_action", "action": action}
                await self._send(handler, cmds)
                dispatched.append(gw_id)
                await self._audit(
                    schedule_id, action, target, "executed",
                    f"manual execute{' (forced)' if force else ''} — "
                    f"{_action_summary(action, params)}",
                )

        status = "fired" if dispatched else ("gated" if gated else "noop")
        return {"status": status, "dispatched": dispatched, "gated": gated}

    async def stop_entry(self, schedule_id: str, now: datetime | None = None) -> dict:
        """Gracefully stop an entry's CURRENT run: release any dispatch it owns
        (honouring its release policy) and mark its current window completed so
        it won't re-fire until the next scheduled window. The entry stays
        ENABLED — this stops *this occurrence*, not the automation. (To stop
        permanently, disable the entry.) Returns
        ``{status, released: [gw_ids]}``."""
        async with self._tick_lock:
            now_dt = now or self._now()
            entry = next((e for e in self._entries if e.get("id") == schedule_id), None)
            released: list[str] = []
            # Release every target this entry currently owns.
            for tkey, own in list(self._owned.items()):
                if own.get("entry_id") != schedule_id:
                    continue
                ttype, tid, gw_id = tkey
                handler = next(
                    (h for g, h in self._resolver(ttype, tid) if g == gw_id), None
                )
                if handler is not None:
                    if own.get("is_v2"):
                        await self._apply_release(
                            handler,
                            own.get("release_policy") or "restore_prior_mode",
                            own.get("prior_mode"),
                        )
                    else:
                        await self._send(handler, [("battery_command", "Release")])
                    released.append(gw_id)
                self._owned.pop(tkey, None)
                self._expired[tkey] = schedule_id  # don't re-fire until next window
                await self._fire_exit_ha_actions(tkey, now_dt)
                await self._audit(
                    schedule_id, own.get("action"), tkey, "stopped",
                    "stopped by user — released; won't re-fire until the next window",
                )
            # Block the current window on any not-yet-owned target too, so a
            # between-ticks fire can't slip through right after Stop.
            if entry is not None:
                ttype = entry.get("target_type", "gateway")
                tid = entry.get("target_id")
                for gw_id, _h in self._resolver(ttype, tid):
                    self._expired.setdefault((ttype, tid, gw_id), schedule_id)
            if released:
                return {"status": "stopped", "released": released}
            return {"status": "not_active" if entry is not None else "not_found", "released": []}

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
        async with self._tick_lock:
            await self._tick_locked(now or self._now())

    async def _tick_locked(self, now: datetime) -> None:
        # Fresh per-tick snapshot cache: each gateway's points are read at most
        # once per tick and shared by every condition-tree evaluation.
        self._snap_cache = {}
        # Drive the connectivity monitor's staleness check (never let it break a
        # tick — recovery/catch-up is best-effort relative to dispatch).
        if self._connectivity is not None:
            try:
                await self._connectivity.tick()
            except Exception as exc:
                logger.debug("Connectivity tick failed: %s", exc)
        # Keep ownership for targets that still have entries; targets dropped
        # entirely (entry deleted) are reconciled to "no desired" below.
        seen: set[tuple] = set()
        for (ttype, tid), entries in self._targets().items():
            # Two lanes. Only a battery command is exclusive — one gateway can
            # run one setpoint at a time — so only those entries contend for the
            # target. HA-actions-only entries command other devices entirely
            # (a solar inverter that may not even be wired through this
            # gateway), so they reconcile under their own key and never block a
            # battery entry. Before this split, an "always evaluate" HA-only
            # entry held the target permanently and starved every schedule on
            # that gateway while dispatching nothing itself.
            battery = [e for e in entries if e.get("action") != "none"]
            ha_only = [e for e in entries if e.get("action") == "none"]
            for gw_id, h in self._resolver(ttype, tid):
                tkey = (ttype, tid, gw_id)
                seen.add(tkey)
                await self._reconcile(tkey, h, await self._battery_winner(battery, tkey, now), now)
                for e in ha_only:
                    # Same gw_id in slot 2 so the audit target still resolves to
                    # the gateway's name; the entry id only isolates the state.
                    hkey = (ttype, f"{tid}#{e['id']}", gw_id)
                    seen.add(hkey)
                    await self._reconcile(hkey, h, e if entry_active(e, now) else None, now)

        # A target/gateway we used to own but whose entries are now gone (or that
        # left a fan-out group) → release it. Re-resolve the handler by gw_id.
        for tkey in list(self._owned):
            if tkey in seen:
                continue
            ttype, tid, gw_id = tkey
            handler = next((h for g, h in self._resolver(ttype, tid) if g == gw_id), None)
            if handler is not None:
                await self._reconcile(tkey, handler, None, now)
            else:
                # gateway is gone — drop our records (nothing to release)
                self._owned.pop(tkey, None)
                self._deferred.discard(tkey)
                self._expired.pop(tkey, None)
                self._gated.discard(tkey)
                self._ha_fired.pop(tkey, None)
                self._exit_entry.pop(tkey, None)
                self._last_win.pop(tkey, None)
                self._skipped = {m for m in self._skipped if m[0] != tkey}

    async def _battery_winner(
        self, entries: list[dict], tkey: tuple, now: datetime
    ) -> dict | None:
        """Which battery entry owns this gateway right now.

        A gateway runs one setpoint at a time, so exactly one entry may hold it.
        Order of preference:

        1. **The incumbent** — an entry we already own keeps the gateway even if
           its gate blips false, so a running dispatch is never dropped and
           re-acquired mid-window.
        2. **The first ranked entry whose entry-conditions pass.** Ranking is
           priority, then oldest-first. Skipping a gated entry is the fix for
           the starvation bug: previously the first *active* entry took the
           gateway and then did nothing when its own conditions failed, while
           every entry behind it went unevaluated and unlogged.
        3. **The first active entry**, when none pass — so a single gated entry
           still audits its own gate exactly as before.
        """
        active = [e for e in entries if entry_active(e, now)]
        if not active:
            self._last_win.pop(tkey, None)
            return None

        own = self._owned.get(tkey)
        if own is not None:
            incumbent = next((e for e in active if e["id"] == own.get("entry_id")), None)
            if incumbent is not None:
                await self._note_skips(active, incumbent, tkey)
                return incumbent

        snap = None
        for e in active:
            tree = e.get("entry_conditions")
            if tree is None:
                await self._note_skips(active, e, tkey)
                return e
            if snap is None:
                snap = self._snapshot(tkey[2], now)
            ok, _trace = eval_conditions(tree, snap)
            if ok:
                await self._note_skips(active, e, tkey)
                return e

        # Nobody's gate passes — hand back the top-ranked entry so it audits its
        # own gate failure (unchanged single-entry behaviour).
        await self._note_skips(active, active[0], tkey)
        return active[0]

    async def _note_skips(self, active: list[dict], win: dict, tkey: tuple) -> None:
        """Audit the entries that lost this gateway, once per contest.

        Losing was previously invisible: a non-winning entry is never evaluated,
        so it wrote nothing to the activity log and simply appeared not to run.
        """
        if len(active) < 2:
            return
        if self._last_win.get(tkey) != win["id"]:
            # New winner → this is a fresh contest; let the losers re-audit.
            self._last_win[tkey] = win["id"]
            self._skipped = {m for m in self._skipped if m[0] != tkey}
        for e in active:
            if e["id"] == win["id"]:
                continue
            mark = (tkey, e["id"], win["id"])
            if mark in self._skipped:
                continue
            self._skipped.add(mark)
            await self._audit(
                e["id"],
                e.get("action", "none"),
                tkey,
                "skipped",
                f"gateway held by '{win.get('name', win['id'])}' this tick",
            )

    async def _reconcile(self, tkey: tuple, handler: Any, win: dict | None, now: datetime) -> None:
        own = self._owned.get(tkey)

        # ── no winning entry: release/hold if we hold a sustained dispatch ──
        if win is None:
            self._expired.pop(tkey, None)  # window over — next entry may re-fire
            self._gated.discard(tkey)  # gate resets; re-entry re-audits if gated
            self._dwell_since.pop(tkey, None)  # dwell timer resets on window exit
            self._dwelling.discard(tkey)
            self._ha_fired.pop(tkey, None)  # re-entry re-fires one-shot HA actions
            await self._fire_exit_ha_actions(tkey, now)  # exit-tagged HA actions on window close
            if own and own["action"] in _SUSTAINED:
                if own["release"] == "hold" and own.get("mode") != "hold":
                    await self._dispatch(handler, "force_standby", {}, label="hold")
                    self._owned[tkey] = {
                        **own,
                        "action": "force_standby",
                        "display": "Force Standby",
                        "mode": "hold",
                    }
                    await self._audit(
                        own.get("entry_id"),
                        "hold",
                        tkey,
                        "ok",
                        "window exit — holding native suppressed",
                    )
                elif own["release"] != "hold":
                    # v2 entries honour release_policy on window/duration exit too
                    # (so a duration-elapsed dispatch restores the prior mode);
                    # legacy entries keep the plain hand-back.
                    policy = own.get("release_policy", "release")
                    if own.get("is_v2") and policy != "release":
                        await self._apply_release(handler, policy, own.get("prior_mode"))
                        detail = f"window exit — released ({policy})"
                    else:
                        await self._send(handler, [("battery_command", "Release")])
                        detail = "window exit — released to native"
                    self._owned.pop(tkey, None)
                    await self._audit(own.get("entry_id"), "release", tkey, "ok", detail)
            else:
                self._owned.pop(tkey, None)
            return

        action = win["action"]
        params = win.get("params", {})
        sig = action_signature(action, params)
        display = _DISPLAY.get(action)

        # ── exit criteria met? intentional in-window termination ──
        # While we actively own a sustained dispatch, a satisfied exit tree
        # (e.g. "battery.soc_pct <= 20") ends the window early and hands back to
        # native. Mark the window completed so it doesn't immediately re-fire —
        # same bound the watchdog-expiry path uses.
        if (
            own
            and own["action"] in _SUSTAINED
            and own.get("mode") != "hold"
            and handler.state.active
        ):
            exit_tree = win.get("exit_conditions")
            if exit_tree is not None:
                met, trace = eval_conditions(exit_tree, self._snapshot(tkey[2], now))
                if met:
                    await self._release_with_policy(handler, win, own)
                    self._owned.pop(tkey, None)
                    self._expired[tkey] = win["id"]
                    await self._audit(
                        win["id"],
                        own["action"],
                        tkey,
                        "exit_condition_met",
                        _condition_reason("exit conditions met", trace),
                    )
                    await self._fire_exit_ha_actions(tkey, now)
                    return

        # ── our sustained dispatch ended in-window (watchdog or external
        #    release)? Mark the window completed and don't re-fire until it's
        #    re-entered. The window — not a per-command watchdog — is the
        #    schedule's bound; without this the engine would re-charge ~1 tick
        #    after a safety release. (hold-mode standby is still active, so it
        #    doesn't trip this.)
        if (
            own
            and own["action"] in _SUSTAINED
            and own.get("mode") != "hold"
            and not handler.state.active
        ):
            self._expired[tkey] = win["id"]
            self._owned.pop(tkey, None)
            await self._audit(
                win["id"],
                own["action"],
                tkey,
                "expired",
                "dispatch ended in-window (watchdog/release) — not re-firing until next window",
            )
            await self._fire_exit_ha_actions(tkey, now)
            return

        # Already completed this entry's current window — hold off.
        if self._expired.get(tkey) == win["id"]:
            return

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
                    await self._audit(
                        win["id"],
                        action,
                        tkey,
                        "deferred",
                        f"target under manual control ({handler.state.action})",
                    )
                return
            # override → fall through and take control (logs a supersede via handler)
        self._deferred.discard(tkey)  # no longer deferring this target

        # ── idempotent: already applying this exact desired state? ──
        if (
            own
            and own["signature"] == sig
            and (action not in _SUSTAINED or handler.state.action == display)
        ):
            return

        # ── entry gate: conditions must hold at fire-time ──
        # Checked only here, on the verge of a (new/changed) dispatch — not while
        # already owning idempotently (that returned above). A failing gate skips
        # the fire and audits once; when it later passes, dispatch proceeds.
        entry_tree = win.get("entry_conditions")
        if entry_tree is not None:
            ok, trace = eval_conditions(entry_tree, self._snapshot(tkey[2], now))
            if not ok:
                self._dwell_since.pop(tkey, None)  # gate failed → reset dwell timer
                self._dwelling.discard(tkey)
                if tkey not in self._gated:
                    self._gated.add(tkey)
                    await self._audit(
                        win["id"],
                        action,
                        tkey,
                        "gated",
                        _condition_reason("entry gated", trace),
                    )
                return
        self._gated.discard(tkey)  # gate passed — clear any prior gated mark

        # ── dwell: eligible (window active + gate passing) continuously for N? ──
        hold_s = int(win.get("entry_hold_s") or 0)
        if hold_s > 0:
            rec = self._dwell_since.get(tkey)
            if rec is None or rec[0] != win["id"]:
                rec = (win["id"], now)
                self._dwell_since[tkey] = rec
            if (now - rec[1]).total_seconds() < hold_s:
                if tkey not in self._dwelling:
                    self._dwelling.add(tkey)
                    await self._audit(
                        win["id"], action, tkey, "waiting",
                        f"conditions met — holding {hold_s}s before firing",
                    )
                return
            self._dwell_since.pop(tkey, None)  # dwell satisfied → fire
            self._dwelling.discard(tkey)

        self._expired.pop(tkey, None)  # fresh dispatch supersedes any old mark
        await self._dispatch(handler, action, params, label=win.get("name", action))
        # One-shot HA actions on this activation edge (after the battery action).
        if self._ha_fired.get(tkey) != win["id"]:
            self._ha_fired[tkey] = win["id"]
            await self._run_ha_actions(win, tkey, "fire", now)
            # Remember the entry so its exit-tagged actions run when it ends.
            if any((a.get("when") == "exit") for a in (win.get("ha_actions") or [])):
                self._exit_entry[tkey] = win
        self._owned[tkey] = {
            "entry_id": win["id"],
            "signature": sig,
            "action": action,
            "display": display,
            "release": win.get("release", "release"),
            "mode": "window",
            # Snapshot the native mode at dispatch so a restore_prior_mode exit
            # can re-assert it (mode.name matches the operating_mode vocabulary).
            "prior_mode": self._snapshot(tkey[2], now).get("mode.name"),
            # v2 entries honour release_policy on ALL release paths (condition
            # exit + duration/window exit); legacy entries never do.
            "is_v2": _is_v2(win),
            "release_policy": win.get("release_policy") or "restore_prior_mode",
        }
        await self._audit(
            win["id"], action, tkey, "ok",
            f"dispatched {win.get('name', action)} — {_action_summary(action, params)}",
        )

    def _guard_ok(self, guard: dict, gw_id: str, now: datetime | None) -> bool:
        """Evaluate an HA action's optional guard leaf against the snapshot."""
        snap = self._snapshot(gw_id, now or self._now())
        ok, _ = eval_conditions({"match": "ALL", "conditions": [guard]}, snap)
        return ok

    async def _run_ha_actions(
        self, win: dict, tkey: tuple, phase: str = "fire", now: datetime | None = None
    ) -> None:
        """Run an entry's one-shot HA-entity actions for the given ``phase`` —
        ``fire`` (on the activation edge, after the battery action) or ``exit``
        (when the window/activation ends). Each is best-effort and audited so one
        failure (HA offline) can't abort the rest or the dispatch. An action with
        an optional ``guard`` leaf runs only if that guard is currently true."""
        actions = [a for a in (win.get("ha_actions") or []) if (a.get("when") or "fire") == phase]
        if not actions:
            return
        if self._ha_action_fn is None:
            await self._audit(
                win["id"], "ha_action", tkey, "failed",
                f"{len(actions)} HA action(s) skipped — no HA configured",
            )
            return
        for a in actions:
            inst = a.get("instance_id")
            eid = a.get("entity_id")
            svc = a.get("service")
            data = a.get("data") or {}
            if not (inst and eid and svc):
                continue
            guard = a.get("guard")
            if guard and not self._guard_ok(guard, tkey[2], now):
                await self._audit(
                    win["id"], "ha_action", tkey, "gated",
                    f"[{phase}] {eid} skipped — guard "
                    f"{guard.get('sensor')}{guard.get('op')}{guard.get('value')} not met",
                )
                continue
            try:
                res = await self._ha_action_fn(inst, eid, svc, data)
            except Exception as exc:  # defensive — call_service already soft-fails
                res = {"ok": False, "error": str(exc) or type(exc).__name__}
            ok = bool(res.get("ok"))
            detail = f"[{phase}] {eid} → {svc}" + (f" {data}" if data else "")
            if not ok:
                detail += f" — {res.get('error')}"
            await self._audit(
                win["id"], "ha_action", tkey, "ha_action" if ok else "failed", detail
            )

    async def _fire_exit_ha_actions(self, tkey: tuple, now: datetime | None = None) -> None:
        """Run any exit-tagged HA actions for a just-ended activation, then forget it."""
        entry = self._exit_entry.pop(tkey, None)
        if entry is not None:
            await self._run_ha_actions(entry, tkey, "exit", now)

    async def _dispatch(self, handler: Any, action: str, params: dict, label: str) -> None:
        if action == "none":
            return  # HA-actions-only entry: no battery command
        cmds = action_to_commands(action, params)
        if not cmds:
            logger.warning("Schedule: unknown action %r — skipped", action)
            return
        logger.info("Schedule dispatch: %s (%s)", label, action)
        await self._send(handler, cmds)

    async def _send(self, handler: Any, cmds: list[tuple[str, str]]) -> None:
        for slug, value in cmds:
            await handler.handle_command(slug, value)

    async def _apply_release(self, handler: Any, policy: str, prior_mode: Any) -> None:
        """Hand VPP control back (``battery_command Release``), then apply the
        release policy: re-assert the prior mode, set a specific mode, or nothing.

        Applies to v2 entries only (condition-exit and v2 duration/window-exit);
        the legacy release/hold path never calls this, so it is unaffected.
        """
        await self._send(handler, [("battery_command", "Release")])
        if policy == "restore_prior_mode":
            if prior_mode:
                await self._send(handler, [("operating_mode", str(prior_mode))])
        elif policy.startswith("set_operating_mode:"):
            target = policy.split(":", 1)[1].strip()
            if target:
                await self._send(handler, [("operating_mode", target)])

    async def _release_with_policy(self, handler: Any, win: dict, own: dict) -> None:
        """Condition-exit release honouring the winning entry's release_policy."""
        await self._apply_release(
            handler,
            win.get("release_policy") or "restore_prior_mode",
            own.get("prior_mode"),
        )

    def _snapshot(self, gw_id: str, now: datetime) -> dict[str, Any]:
        """Sensor snapshot for a gateway, cached per tick. No points source (or a
        failing one) yields an all-None snapshot, so conditions fail closed."""
        cached = self._snap_cache.get(gw_id)
        if cached is not None:
            return cached
        pts: dict = {}
        if self._points_fn is not None:
            try:
                pts = self._points_fn(gw_id) or {}
            except Exception as exc:
                logger.debug("Schedule: points_fn(%s) failed: %s", gw_id, exc)
                pts = {}
        snap = sensor_snapshot(pts, now)
        self._snap_cache[gw_id] = snap
        return snap

    async def _audit(
        self,
        schedule_id: str | None,
        action: str,
        target: str,
        result: str,
        detail: str,
    ) -> None:
        if self._on_audit is None:
            return
        try:
            await self._on_audit(schedule_id, action, self._target_label(target), result, detail)
        except Exception as exc:
            logger.debug("Schedule audit failed: %s", exc)

    def _target_label(self, target: Any) -> str:
        """Readable target for the audit column. A ``(ttype, tid, gw_id)`` tuple
        renders as the resolved gateway's human name (+ "(mock)" flag) so a
        simulator target is obvious; anything else is stringified as-is."""
        if isinstance(target, (tuple, list)) and len(target) == 3:
            gw_id = target[2]
            if self._gw_label_fn is not None:
                try:
                    return self._gw_label_fn(gw_id)
                except Exception:
                    pass
            return str(gw_id)
        return str(target)
