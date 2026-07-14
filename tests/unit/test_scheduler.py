"""Schedule engine (SCH1) — window logic, winner selection, conflict policy,
release/gap behaviour, idempotency. No controller, registry, or hardware:
the engine drives fake command handlers and an in-memory store.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.scheduler import (
    ScheduleEngine,
    action_signature,
    action_to_commands,
    entry_active_at,
    entry_date,
    next_fire,
    parse_when,
    winner,
)
from franklinwh_bridge.store.db import (
    create_schedule,
    delete_schedule,
    get_schedule,
    get_schedules,
    init_db,
    update_schedule,
)

MON = datetime(2026, 6, 15, 10, 30)  # a Monday, 10:30
SUN = datetime(2026, 6, 14, 10, 30)  # a Sunday, 10:30


# ── pure window helpers ───────────────────────────────────────


def test_parse_when_days_and_windows():
    days, windows = parse_when({"days": [0, 2, 4], "windows": [{"start": "08:00", "end": "10:00"}]})
    assert days == {0, 2, 4}
    assert windows == [(480, 600)]


def test_active_inside_window_on_matching_day():
    spec = {"days": [0], "windows": [{"start": "10:00", "end": "11:00"}]}
    assert entry_active_at(spec, MON) is True


def test_inactive_outside_window():
    spec = {"days": [0], "windows": [{"start": "11:00", "end": "12:00"}]}
    assert entry_active_at(spec, MON) is False


def test_inactive_on_wrong_weekday():
    spec = {"days": [0], "windows": [{"start": "10:00", "end": "11:00"}]}
    assert entry_active_at(spec, SUN) is False  # Sunday, days=[Mon]


def test_empty_days_means_every_day():
    spec = {"windows": [{"start": "10:00", "end": "11:00"}]}
    assert entry_active_at(spec, MON) is True
    assert entry_active_at(spec, SUN) is True


def test_window_wraps_past_midnight():
    spec = {"windows": [{"start": "22:00", "end": "06:00"}]}
    assert entry_active_at(spec, datetime(2026, 6, 15, 23, 0)) is True
    assert entry_active_at(spec, datetime(2026, 6, 15, 3, 0)) is True
    assert entry_active_at(spec, datetime(2026, 6, 15, 12, 0)) is False


def test_end_equals_start_matches_nothing():
    spec = {"windows": [{"start": "10:00", "end": "10:00"}]}
    assert entry_active_at(spec, MON) is False


def test_next_fire_same_day_later():
    spec = {"windows": [{"start": "14:00", "end": "15:00"}]}
    nf = next_fire(spec, MON)
    assert nf == datetime(2026, 6, 15, 14, 0)


def test_next_fire_rolls_to_matching_weekday():
    # Only Wednesday (2); from Monday it should land on Wed.
    spec = {"days": [2], "windows": [{"start": "08:00", "end": "09:00"}]}
    nf = next_fire(spec, MON)
    assert nf.weekday() == 2
    assert nf.hour == 8


def test_next_fire_none_without_windows():
    assert next_fire({"days": [0]}, MON) is None


# ── one-time (dated) entries ────────────────────────────────────


def test_entry_date_parses_iso_string():
    assert entry_date({"date": "2026-06-15"}) == datetime(2026, 6, 15).date()
    assert entry_date({}) is None
    assert entry_date({"date": "not-a-date"}) is None


def test_dated_entry_active_on_its_date():
    spec = {"date": "2026-06-15", "windows": [{"start": "10:00", "end": "11:00"}]}
    assert entry_active_at(spec, MON) is True  # MON is 2026-06-15


def test_dated_entry_does_not_refire_next_week_same_weekday():
    """The exact bug being fixed: a one-time entry must not behave like a
    recurring weekly rule just because the date happens to share a weekday."""
    spec = {"date": "2026-06-15", "windows": [{"start": "10:00", "end": "11:00"}]}
    next_monday = datetime(2026, 6, 22, 10, 30)
    assert entry_active_at(spec, next_monday) is False


def test_dated_entry_inactive_day_before_and_after():
    spec = {"date": "2026-06-15", "windows": [{"start": "10:00", "end": "11:00"}]}
    assert entry_active_at(spec, datetime(2026, 6, 14, 10, 30)) is False
    assert entry_active_at(spec, datetime(2026, 6, 16, 10, 30)) is False


def test_dated_entry_ignores_days_field():
    # date wins even if a (stale/unused) days list would otherwise exclude it.
    spec = {"date": "2026-06-15", "days": [5], "windows": [{"start": "10:00", "end": "11:00"}]}
    assert entry_active_at(spec, MON) is True


def test_next_fire_for_dated_entry_same_day():
    spec = {"date": "2026-06-15", "windows": [{"start": "14:00", "end": "15:00"}]}
    assert next_fire(spec, MON) == datetime(2026, 6, 15, 14, 0)


def test_next_fire_for_dated_entry_beyond_horizon():
    spec = {"date": "2026-07-01", "windows": [{"start": "09:00", "end": "10:00"}]}
    nf = next_fire(spec, MON, horizon_days=8)  # 2026-07-01 is >8 days out
    assert nf == datetime(2026, 7, 1, 9, 0)


def test_next_fire_none_once_dated_entry_has_passed():
    spec = {"date": "2026-06-01", "windows": [{"start": "09:00", "end": "10:00"}]}
    assert next_fire(spec, MON) is None


async def test_dated_entry_fires_only_on_its_own_date_across_restarts():
    """Simulates the reported bug: an engine restarting a week later at the
    same time-of-day must not re-dispatch a one-time entry."""
    spec = {"date": "2026-06-15", "windows": [{"start": "10:00", "end": "11:00"}]}
    h = FakeHandler()
    eng = _engine([_entry(when_spec=spec)], h)
    await eng.tick(MON)
    assert h.state.action == "Force Charge"

    h2 = FakeHandler()
    eng2 = _engine([_entry(when_spec=spec)], h2)
    await eng2.tick(datetime(2026, 6, 22, 10, 30))  # restart, one week later
    assert h2.state.active is False


# ── action translation ────────────────────────────────────────


def test_force_charge_sets_power_then_command():
    cmds = action_to_commands("force_charge", {"power_w": 1500})
    assert cmds == [("battery_command_power", "1500"), ("battery_command", "Force Charge")]


def test_force_discharge_pct():
    cmds = action_to_commands("force_discharge", {"power_pct": 50})
    assert cmds == [("battery_command_power_pct", "50"), ("battery_command", "Force Discharge")]


def test_reserve_and_mode_actions():
    assert action_to_commands("reserve_self", {"pct": 30}) == [("self_reserve_pct", "30")]
    assert action_to_commands("mode", {"mode": "TOU"}) == [("operating_mode", "TOU")]


def test_unknown_action_is_empty():
    assert action_to_commands("nope", {}) == []


def test_signature_changes_with_power():
    a = action_signature("force_charge", {"power_w": 1000})
    b = action_signature("force_charge", {"power_w": 2000})
    assert a != b


# ── winner selection ──────────────────────────────────────────


def test_winner_is_first_active_in_priority_order():
    # Store sorts priority desc; the engine takes the first *active* one.
    entries = [
        {"id": "hi", "when_spec": {"windows": [{"start": "10:00", "end": "11:00"}]}},
        {"id": "lo", "when_spec": {"windows": [{"start": "10:00", "end": "11:00"}]}},
    ]
    assert winner(entries, MON)["id"] == "hi"


def test_winner_skips_inactive():
    entries = [
        {"id": "hi", "when_spec": {"windows": [{"start": "20:00", "end": "21:00"}]}},
        {"id": "lo", "when_spec": {"windows": [{"start": "10:00", "end": "11:00"}]}},
    ]
    assert winner(entries, MON)["id"] == "lo"


def test_winner_none_when_all_inactive():
    entries = [{"id": "x", "when_spec": {"windows": [{"start": "20:00", "end": "21:00"}]}}]
    assert winner(entries, MON) is None


# ── engine with fake handlers ─────────────────────────────────


class FakeHandler:
    """Minimal CommandHandler stand-in: records commands, tracks active/action."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

        class _S:
            active = False
            action = ""

        self.state = _S()

    async def handle_command(self, slug: str, value: str) -> None:
        self.calls.append((slug, value))
        if slug == "battery_command":
            if value in ("Release", "Not Active", "Stop"):
                self.state.active = False
                self.state.action = ""
            else:
                base = value[6:] if value.lower().startswith("force ") else value
                self.state.active = True
                self.state.action = f"Force {base}"


def _entry(**kw):
    base = {
        "id": "e1", "name": "e1", "enabled": True,
        "when_spec": {"windows": [{"start": "10:00", "end": "11:00"}]},
        "action": "force_charge", "params": {"power_w": 1000},
        "target_type": "gateway", "target_id": "default",
        "release": "release", "conflict": "defer", "priority": 0,
    }
    base.update(kw)
    return base


def _engine(entries, handler):
    eng = ScheduleEngine(db=None, resolver=lambda tt, tid: [("default", handler)])
    eng._entries = entries
    return eng


async def test_dispatch_on_window_enter():
    h = FakeHandler()
    eng = _engine([_entry()], h)
    await eng.tick(MON)
    assert ("battery_command", "Force Charge") in h.calls
    assert h.state.action == "Force Charge"


async def test_idempotent_no_redispatch():
    h = FakeHandler()
    eng = _engine([_entry()], h)
    await eng.tick(MON)
    n = len(h.calls)
    await eng.tick(MON)  # still in-window, same desired → no new commands
    assert len(h.calls) == n


async def test_release_on_window_exit():
    h = FakeHandler()
    eng = _engine([_entry(release="release")], h)
    await eng.tick(MON)  # enter → Force Charge
    await eng.tick(datetime(2026, 6, 15, 12, 0))  # exit window
    assert h.calls[-1] == ("battery_command", "Release")
    assert h.state.active is False


async def test_hold_keeps_standby_across_gap():
    h = FakeHandler()
    eng = _engine([_entry(release="hold")], h)
    await eng.tick(MON)  # Force Charge
    await eng.tick(datetime(2026, 6, 15, 12, 0))  # exit → hold = standby, not release
    assert ("battery_command", "Force Standby") in h.calls
    assert h.calls[-1] != ("battery_command", "Release")
    assert h.state.active is True  # still under VPP standby (native suppressed)


async def test_conflict_defer_leaves_manual_alone():
    h = FakeHandler()
    # Simulate a manual Force Discharge already active.
    h.state.active = True
    h.state.action = "Force Discharge"
    eng = _engine([_entry(action="force_charge", conflict="defer")], h)
    await eng.tick(MON)
    # No battery_command issued — manual control respected.
    assert not any(c[0] == "battery_command" for c in h.calls)
    assert h.state.action == "Force Discharge"


async def test_conflict_override_preempts_manual():
    h = FakeHandler()
    h.state.active = True
    h.state.action = "Force Discharge"
    eng = _engine([_entry(action="force_charge", conflict="override")], h)
    await eng.tick(MON)
    assert h.state.action == "Force Charge"


async def test_power_change_redispatches():
    h = FakeHandler()
    e = _entry(params={"power_w": 1000})
    eng = _engine([e], h)
    await eng.tick(MON)
    n = len(h.calls)
    e["params"] = {"power_w": 2000}  # entry edited
    await eng.tick(MON)
    assert len(h.calls) > n
    assert ("battery_command_power", "2000") in h.calls


async def test_concurrent_ticks_dispatch_once():
    """reload()'s immediate tick + the loop tick must not double-command the
    battery (regression from live LT-1: two identical Force Charge dispatches)."""
    import asyncio

    h = FakeHandler()
    eng = _engine([_entry()], h)
    await asyncio.gather(eng.tick(MON), eng.tick(MON))  # race two ticks
    cmds = [c for c in h.calls if c[0] == "battery_command"]
    assert cmds == [("battery_command", "Force Charge")]  # exactly one dispatch


async def test_watchdog_release_does_not_refire_within_window():
    """If a sustained dispatch ends in-window (watchdog/external release), the
    engine must NOT re-fire until the window is re-entered (LT-10)."""
    h = FakeHandler()
    eng = _engine([_entry()], h)
    await eng.tick(MON)                       # dispatch Force Charge
    assert h.state.action == "Force Charge"
    h.state.active = False                    # simulate watchdog release
    h.state.action = ""
    await eng.tick(MON)                        # still in-window…
    n = len(h.calls)
    await eng.tick(MON)                        # …and again
    assert len(h.calls) == n                   # no re-dispatch
    assert h.state.active is False


async def test_refires_after_window_re_enter():
    """An expired window clears once the window exits, so the next entry into
    the window dispatches again."""
    h = FakeHandler()
    eng = _engine([_entry()], h)
    await eng.tick(MON)
    h.state.active = False; h.state.action = ""   # watchdog release
    await eng.tick(MON)                            # marks window expired
    await eng.tick(datetime(2026, 6, 15, 12, 0))  # window exit clears it
    await eng.tick(MON)                            # re-enter → dispatch again
    assert h.state.action == "Force Charge"


async def test_deleted_entry_releases_owned_target():
    h = FakeHandler()
    eng = _engine([_entry()], h)
    await eng.tick(MON)  # owns the target
    eng._entries = []  # entry removed
    await eng.tick(MON)
    assert h.calls[-1] == ("battery_command", "Release")


# ── service / site fan-out (SCH3) ─────────────────────────────


def _fanout_engine(entries, members):
    """members: dict gw_id -> FakeHandler. Resolver fans a service/site target
    out to all of them; a gateway target resolves just that one."""
    def resolver(ttype, tid):
        if ttype == "gateway":
            h = members.get(tid)
            return [(tid, h)] if h else []
        return [(gid, h) for gid, h in members.items()]
    eng = ScheduleEngine(db=None, resolver=resolver)
    eng._entries = entries
    return eng


async def test_service_target_fans_out_to_all_members():
    a, b = FakeHandler(), FakeHandler()
    e = _entry(target_type="service", target_id="svc_x")
    eng = _fanout_engine([e], {"gwA": a, "gwB": b})
    await eng.tick(MON)
    assert a.state.action == "Force Charge"
    assert b.state.action == "Force Charge"


async def test_fanout_releases_all_on_window_exit():
    a, b = FakeHandler(), FakeHandler()
    e = _entry(target_type="site", target_id=None)
    eng = _fanout_engine([e], {"gwA": a, "gwB": b})
    await eng.tick(MON)
    await eng.tick(datetime(2026, 6, 15, 12, 0))  # window exit
    assert a.calls[-1] == ("battery_command", "Release")
    assert b.calls[-1] == ("battery_command", "Release")


async def test_fanout_ownership_is_per_gateway():
    """A member leaving the group must not shift another member's ownership
    (the bug index-based keys would cause)."""
    a, b = FakeHandler(), FakeHandler()
    members = {"gwA": a, "gwB": b}
    e = _entry(target_type="site", target_id=None)
    eng = _fanout_engine([e], members)
    await eng.tick(MON)                      # both owned + charging
    members.pop("gwA")                        # gwA leaves the group
    await eng.tick(MON)
    # gwB stays charging on its own key; gwA untouched after leaving
    assert b.state.action == "Force Charge"
    nb = len(b.calls)
    await eng.tick(MON)
    assert len(b.calls) == nb                 # idempotent for the survivor


# ── store CRUD round-trip ─────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "sched.db")
    yield conn
    await conn.close()


async def test_store_create_and_decode(db):
    row = await create_schedule(
        db, name="Peak charge",
        when_spec={"days": [0, 1], "windows": [{"start": "14:00", "end": "19:00"}]},
        action="force_charge", params={"power_w": 2000},
        target_type="gateway", target_id="default",
        release="hold", conflict="override", priority=5,
    )
    assert row["id"].startswith("sch_")
    assert row["when_spec"]["windows"][0]["start"] == "14:00"
    assert row["params"]["power_w"] == 2000
    assert row["enabled"] is True
    assert row["release"] == "hold"


async def test_store_update_json_field(db):
    row = await create_schedule(
        db, name="x", when_spec={"windows": [{"start": "01:00", "end": "02:00"}]},
        action="force_standby",
    )
    updated = await update_schedule(
        db, row["id"], params={"duration_s": 600}, enabled=False,
    )
    assert updated["params"]["duration_s"] == 600
    assert updated["enabled"] is False


async def test_store_sorted_by_priority(db):
    await create_schedule(db, name="lo", when_spec={}, action="force_standby", priority=1)
    await create_schedule(db, name="hi", when_spec={}, action="force_standby", priority=9)
    rows = await get_schedules(db)
    assert rows[0]["name"] == "hi"


async def test_store_delete(db):
    row = await create_schedule(db, name="x", when_spec={}, action="force_standby")
    assert await delete_schedule(db, row["id"]) is True
    assert await get_schedule(db, row["id"]) is None
    assert await delete_schedule(db, row["id"]) is False
