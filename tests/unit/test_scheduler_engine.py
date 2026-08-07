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
