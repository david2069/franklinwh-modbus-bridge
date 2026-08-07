"""Scheduler v2 engine wiring — entry-condition gate, exit criteria, and the
per-gateway sensor snapshot. Layers onto the existing window model; legacy
(condition-free) entries are exercised by test_scheduler.py and must stay
unaffected. No controller, registry, or hardware — fake handlers + injected
points.
"""

from __future__ import annotations

from datetime import datetime

from franklinwh_bridge.gateway.scheduler import ScheduleEngine

MON = datetime(2026, 6, 15, 10, 30)  # a Monday, inside the 10:00–11:00 window
GT = {"sensor": "battery.soc_pct", "op": ">", "value": 50}
LE20 = {"sensor": "battery.soc_pct", "op": "<=", "value": 20}


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


def entry(**kw):
    base = {
        "id": "e1",
        "name": "e1",
        "enabled": True,
        "when_spec": {"windows": [{"start": "10:00", "end": "11:00"}]},
        "action": "force_charge",
        "params": {"power_w": 1000},
        "target_type": "gateway",
        "target_id": "default",
        "release": "release",
        "conflict": "defer",
        "priority": 0,
        "entry_conditions": None,
        "exit_conditions": None,
    }
    base.update(kw)
    return base


def make_engine(entries, handler, points=None, audits=None):
    """points: a (mutable) dict of raw sample points, or None for no source.
    audits: a list to capture audit rows, or None to disable auditing."""

    async def on_audit(sid, action, target, result, detail):
        audits.append({"id": sid, "action": action, "result": result, "detail": detail})

    eng = ScheduleEngine(
        db=None,
        resolver=lambda tt, tid: [("default", handler)],
        on_audit=on_audit if audits is not None else None,
        points_fn=(lambda gw: points) if points is not None else None,
    )
    eng._entries = entries
    return eng


def gated_count(audits):
    return sum(1 for a in audits if a["result"] == "gated")


# ── entry-condition gate ──────────────────────────────────────


async def test_entry_gate_pass_dispatches():
    h = FakeHandler()
    e = entry(entry_conditions={"conditions": [GT]})
    eng = make_engine([e], h, points={"soc": 80})
    await eng.tick(MON)
    assert h.state.action == "Force Charge"


async def test_entry_gate_fail_skips_and_audits_gated():
    h = FakeHandler()
    audits: list = []
    e = entry(entry_conditions={"conditions": [GT]})
    eng = make_engine([e], h, points={"soc": 30}, audits=audits)
    await eng.tick(MON)
    assert h.state.active is False
    assert not any(c[0] == "battery_command" for c in h.calls)
    assert gated_count(audits) == 1
    assert "battery.soc_pct" in audits[-1]["detail"]


async def test_gate_audits_once_while_gated_then_dispatches_when_it_passes():
    h = FakeHandler()
    audits: list = []
    pts = {"soc": 30}
    e = entry(entry_conditions={"conditions": [GT]})
    eng = make_engine([e], h, points=pts, audits=audits)
    await eng.tick(MON)
    await eng.tick(MON)  # still gated — must not re-audit every tick
    assert gated_count(audits) == 1
    pts["soc"] = 80  # conditions now hold
    await eng.tick(MON)
    assert h.state.action == "Force Charge"


async def test_entry_conditions_fail_closed_without_points_source():
    h = FakeHandler()
    audits: list = []
    e = entry(entry_conditions={"conditions": [GT]})
    eng = make_engine([e], h, points=None, audits=audits)  # no points_fn
    await eng.tick(MON)
    assert h.state.active is False
    assert gated_count(audits) == 1


async def test_legacy_entry_unaffected_with_points_source():
    h = FakeHandler()
    e = entry()  # no conditions
    eng = make_engine([e], h, points={"soc": 10})
    await eng.tick(MON)
    assert h.state.action == "Force Charge"


# ── exit criteria ─────────────────────────────────────────────


async def test_exit_condition_releases_and_audits():
    h = FakeHandler()
    audits: list = []
    pts = {"soc": 80}
    e = entry(action="force_discharge", exit_conditions={"conditions": [LE20]})
    eng = make_engine([e], h, points=pts, audits=audits)
    await eng.tick(MON)
    assert h.state.action == "Force Discharge"
    pts["soc"] = 15  # cross the exit threshold
    await eng.tick(MON)
    assert h.calls[-1] == ("battery_command", "Release")
    assert h.state.active is False
    assert any(a["result"] == "exit_condition_met" for a in audits)


async def test_exit_condition_does_not_refire_within_window():
    h = FakeHandler()
    pts = {"soc": 80}
    e = entry(action="force_discharge", exit_conditions={"conditions": [LE20]})
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)
    pts["soc"] = 15
    await eng.tick(MON)  # exit → release, mark window expired
    n = len(h.calls)
    await eng.tick(MON)  # still in-window, must NOT re-fire
    assert len(h.calls) == n
    assert h.state.active is False


async def test_exit_condition_not_met_stays_active():
    h = FakeHandler()
    pts = {"soc": 80}
    e = entry(action="force_discharge", exit_conditions={"conditions": [LE20]})
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)
    await eng.tick(MON)  # soc still 80, exit not met
    assert h.state.action == "Force Discharge"


async def test_refires_after_window_reenter_following_exit():
    h = FakeHandler()
    pts = {"soc": 80}
    e = entry(action="force_discharge", exit_conditions={"conditions": [LE20]})
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)  # discharge
    pts["soc"] = 15
    await eng.tick(MON)  # exit → release + expired
    assert h.state.active is False
    await eng.tick(datetime(2026, 6, 15, 12, 0))  # window exit clears expired
    pts["soc"] = 80
    await eng.tick(MON)  # re-enter window → dispatch again
    assert h.state.action == "Force Discharge"


# ── release policy on the exit path ───────────────────────────


async def test_exit_restore_prior_mode_reasserts_captured_mode():
    h = FakeHandler()
    pts = {"soc": 80, "mode_name": "Self-Consumption"}
    e = entry(
        action="force_discharge",
        exit_conditions={"conditions": [LE20]},
        release_policy="restore_prior_mode",
    )
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)  # dispatch — captures prior mode Self-Consumption
    pts["soc"] = 15
    await eng.tick(MON)  # exit → Release, then restore mode
    assert ("battery_command", "Release") in h.calls
    assert ("operating_mode", "Self-Consumption") in h.calls
    # Release must come before the mode re-assert.
    assert h.calls.index(("battery_command", "Release")) < h.calls.index(
        ("operating_mode", "Self-Consumption")
    )


async def test_exit_set_operating_mode_policy():
    h = FakeHandler()
    pts = {"soc": 80, "mode_name": "Self-Consumption"}
    e = entry(
        action="force_discharge",
        exit_conditions={"conditions": [LE20]},
        release_policy="set_operating_mode:TOU",
    )
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)
    pts["soc"] = 15
    await eng.tick(MON)
    assert ("operating_mode", "TOU") in h.calls


async def test_exit_release_policy_plain_release_no_mode_command():
    h = FakeHandler()
    pts = {"soc": 80, "mode_name": "Self-Consumption"}
    e = entry(
        action="force_discharge",
        exit_conditions={"conditions": [LE20]},
        release_policy="release",
    )
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)
    pts["soc"] = 15
    await eng.tick(MON)
    assert ("battery_command", "Release") in h.calls
    assert not any(c[0] == "operating_mode" for c in h.calls)


async def test_exit_restore_prior_mode_noop_when_mode_unknown_at_dispatch():
    # No mode_name in the snapshot at dispatch → nothing to restore, just Release.
    h = FakeHandler()
    pts = {"soc": 80}
    e = entry(
        action="force_discharge",
        exit_conditions={"conditions": [LE20]},
        release_policy="restore_prior_mode",
    )
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)
    pts["soc"] = 15
    await eng.tick(MON)
    assert ("battery_command", "Release") in h.calls
    assert not any(c[0] == "operating_mode" for c in h.calls)


async def test_first_tick_dispatches_even_if_exit_already_true():
    # Exit is a while-active termination, not an entry gate: one dispatch happens,
    # then the next tick releases. (Use an entry_condition to gate the start.)
    h = FakeHandler()
    pts = {"soc": 15}
    e = entry(action="force_discharge", exit_conditions={"conditions": [LE20]})
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)
    assert h.state.action == "Force Discharge"  # started
    await eng.tick(MON)
    assert h.state.active is False  # then exited


# ── fire-based triggers (slice 3) ─────────────────────────────

# MON is 2026-06-15 10:30 (a Monday), used by the tests above.


async def test_daily_trigger_active_dispatches():
    h = FakeHandler()
    e = entry(trigger_kind="daily", trigger_spec={"time_of_day": "09:00"}, duration_s=7200)
    eng = make_engine([e], h, points={"soc": 50})
    await eng.tick(MON)  # 10:30 within [09:00, 11:00)
    assert h.state.action == "Force Charge"


async def test_trigger_inactive_after_duration_window():
    h = FakeHandler()
    e = entry(trigger_kind="daily", trigger_spec={"time_of_day": "09:00"}, duration_s=1800)
    eng = make_engine([e], h)
    await eng.tick(MON)  # 10:30 is past 09:30 → not active
    assert h.state.active is False


async def test_interval_trigger_active_dispatches():
    h = FakeHandler()
    e = entry(trigger_kind="interval", trigger_spec={"every_seconds": 3600}, duration_s=3600)
    eng = make_engine([e], h, points={"soc": 50})
    await eng.tick(MON)  # prev fire 10:00 (midnight anchor), active until 11:00
    assert h.state.action == "Force Charge"


async def test_oneoff_trigger_dispatch_then_duration_release():
    h = FakeHandler()
    e = entry(
        action="force_discharge",
        trigger_kind="oneoff",
        trigger_spec={"fire_at": "2026-06-15T10:30:00"},
        duration_s=1800,
        release_policy="release",
    )
    eng = make_engine([e], h, points={"soc": 80})
    await eng.tick(MON)  # fire at 10:30 → dispatch
    assert h.state.action == "Force Discharge"
    await eng.tick(datetime(2026, 6, 15, 11, 1))  # past 11:00 end → release
    assert h.state.active is False
    assert ("battery_command", "Release") in h.calls


async def test_duration_elapsed_restores_prior_mode_for_v2():
    h = FakeHandler()
    pts = {"soc": 80, "mode_name": "Self-Consumption"}
    e = entry(
        action="force_discharge",
        trigger_kind="oneoff",
        trigger_spec={"fire_at": "2026-06-15T10:30:00"},
        duration_s=1800,
        release_policy="restore_prior_mode",
    )
    eng = make_engine([e], h, points=pts)
    await eng.tick(MON)  # dispatch, capture prior mode
    assert h.state.action == "Force Discharge"
    await eng.tick(datetime(2026, 6, 15, 11, 1))  # duration elapsed → release + restore
    assert ("battery_command", "Release") in h.calls
    assert ("operating_mode", "Self-Consumption") in h.calls


async def test_always_trigger_gated_by_entry_conditions():
    # always is continuously active; entry_conditions decide dispatch.
    h_ok = FakeHandler()
    e_ok = entry(trigger_kind="always", entry_conditions={"conditions": [GT]})
    await make_engine([e_ok], h_ok, points={"soc": 80}).tick(MON)
    assert h_ok.state.action == "Force Charge"

    h_no = FakeHandler()
    e_no = entry(trigger_kind="always", entry_conditions={"conditions": [GT]})
    await make_engine([e_no], h_no, points={"soc": 30}).tick(MON)
    assert h_no.state.active is False


async def test_legacy_window_exit_still_plain_release():
    # A legacy (non-v2) entry must NOT restore a mode on window exit.
    h = FakeHandler()
    e = entry(when_spec={"windows": [{"start": "10:00", "end": "11:00"}]})
    eng = make_engine([e], h, points={"soc": 50, "mode_name": "Self-Consumption"})
    await eng.tick(MON)
    await eng.tick(datetime(2026, 6, 15, 12, 0))  # window exit
    assert h.calls[-1] == ("battery_command", "Release")  # nothing after Release
    assert not any(c[0] == "operating_mode" for c in h.calls)


# ── outage catch-up (Phase 2 B) ───────────────────────────────

# since_ts / now chosen around MON (2026-06-15 10:30).
import time as _time  # noqa: E402

_SINCE = _time.mktime(datetime(2026, 6, 15, 9, 0).timetuple())  # 09:00 local


async def test_catchup_audits_fully_passed_trigger_as_missed():
    h = FakeHandler()
    audits: list = []
    # oneoff fired at 09:30 for 30 min → window [09:30, 10:00], fully passed by 10:30.
    e = entry(
        trigger_kind="oneoff", trigger_spec={"fire_at": "2026-06-15T09:30:00"},
        duration_s=1800, when_spec={},
    )
    eng = make_engine([e], h, audits=audits)
    missed = await eng.catchup("default", _SINCE, now=MON)
    assert missed == ["e1"]
    assert any(a["result"] == "missed" for a in audits)


async def test_catchup_skips_still_open_window():
    h = FakeHandler()
    audits: list = []
    # oneoff fired at 10:00 for 2h → window still open at 10:30 → NOT missed
    # (the normal tick resumes it).
    e = entry(
        trigger_kind="oneoff", trigger_spec={"fire_at": "2026-06-15T10:00:00"},
        duration_s=7200, when_spec={},
    )
    eng = make_engine([e], h, audits=audits)
    missed = await eng.catchup("default", _SINCE, now=MON)
    assert missed == []
    assert not any(a["result"] == "missed" for a in audits)


async def test_catchup_no_fire_during_outage_window():
    h = FakeHandler()
    # oneoff fired at 08:00 (before the 09:00 outage start) → not attributable.
    e = entry(
        trigger_kind="oneoff", trigger_spec={"fire_at": "2026-06-15T08:00:00"},
        duration_s=600, when_spec={},
    )
    eng = make_engine([e], h)
    missed = await eng.catchup("default", _SINCE, now=MON)
    assert missed == []


async def test_catchup_legacy_window_missed():
    h = FakeHandler()
    audits: list = []
    # legacy window 09:15–09:45, fully passed by 10:30.
    e = entry(when_spec={"windows": [{"start": "09:15", "end": "09:45"}]})
    eng = make_engine([e], h, audits=audits)
    missed = await eng.catchup("default", _SINCE, now=MON)
    assert missed == ["e1"]
    assert any(a["result"] == "missed" for a in audits)


async def test_catchup_ignores_other_gateway_targets():
    h = FakeHandler()
    e = entry(
        trigger_kind="oneoff", trigger_spec={"fire_at": "2026-06-15T09:30:00"},
        duration_s=600, when_spec={},
    )
    # Resolver maps this entry to gateway "gwX", not "default".
    eng = ScheduleEngine(db=None, resolver=lambda tt, tid: [("gwX", h)])
    eng._entries = [e]
    missed = await eng.catchup("default", _SINCE, now=MON)  # recovered gw ≠ gwX
    assert missed == []
