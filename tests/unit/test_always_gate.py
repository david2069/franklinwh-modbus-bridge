"""An Always entry's entry conditions are its window (#165): a run ends when they turn
false, and the entry fires again on the next true, after an exit, Stop or time limit
too. Before the fix a running force ignored the gate, and once a run ended the entry
never fired again until the bridge restarted."""

from __future__ import annotations

from datetime import timedelta

import pytest

from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.store.db import create_schedule, init_db, update_schedule
from tests.unit.test_scheduler_engine import GT, MON, FakeHandler, entry, make_engine


def always(**kw):
    return entry(trigger_kind="always", when_spec={}, entry_conditions={"conditions": [GT]}, **kw)


def forces(h):
    return [v for s, v in h.calls if s == "battery_command" and v != "Release"]


async def run(eng, points, socs, t=MON):
    for soc in socs:
        points["soc"] = soc
        t += timedelta(seconds=15)
        await eng.tick(t)
    return t


async def test_force_is_released_when_the_gate_turns_false():
    h, p, audits = FakeHandler(), {"soc": 80}, []
    eng = make_engine([always()], h, points=p, audits=audits)
    await run(eng, p, [80, 80, 30])
    assert h.calls[-1] == ("battery_command", "Release")
    assert h.state.active is False
    assert any("entry conditions no longer met" in a["detail"] for a in audits)


async def test_fires_again_after_an_exit_once_the_gate_has_been_false():
    h, p = FakeHandler(), {"soc": 80}
    exit_low = {"sensor": "battery.soc_pct", "op": "<", "value": 60}
    eng = make_engine([always(exit_conditions={"conditions": [exit_low]})], h, points=p)
    t = await run(eng, p, [80, 55, 55])  # exit at 55 (gate > 50 still true)
    assert len(forces(h)) == 1 and h.state.active is False
    await run(eng, p, [40, 80], t)
    assert len(forces(h)) == 2


async def test_fires_again_on_the_next_true():
    h, p = FakeHandler(), {"soc": 80}
    eng = make_engine([always()], h, points=p)
    await run(eng, p, [80, 30, 30, 80])
    assert len(forces(h)) == 2


async def test_fires_again_after_a_time_limit_once_the_gate_has_been_false():
    h, p = FakeHandler(), {"soc": 80}
    eng = make_engine([always()], h, points=p)
    t = await run(eng, p, [80])
    h.state.active = False  # the command's own time limit ended it
    t = await run(eng, p, [80, 80, 80], t)
    assert len(forces(h)) == 1  # done while the gate stays true
    await run(eng, p, [30, 80], t)
    assert len(forces(h)) == 2


async def test_fires_again_after_stop_once_the_gate_has_been_false():
    h, p = FakeHandler(), {"soc": 80}
    eng = make_engine([always()], h, points=p)
    t = await run(eng, p, [80])
    await eng.stop_entry("e1", now=t)
    t = await run(eng, p, [80, 80], t)
    assert len(forces(h)) == 1
    await run(eng, p, [30, 80], t)
    assert len(forces(h)) == 2


async def test_ha_only_always_entry_fires_on_each_true_and_exits_on_false():
    calls = []

    async def ha(inst, eid, svc, data):
        calls.append(svc)
        return {"ok": True}

    acts = [{"kind": "action", "instance_id": "ha1", "entity_id": "switch.pool",
             "service": "turn_on", "when": "fire"},
            {"kind": "action", "instance_id": "ha1", "entity_id": "switch.pool",
             "service": "turn_off", "when": "exit"}]
    h, p = FakeHandler(), {"soc": 80}
    eng = ScheduleEngine(db=None, resolver=lambda tt, tid: [("default", h)],
                         points_fn=lambda gw: p, ha_action_fn=ha)
    eng._entries = [always(action="none", params={}, ha_actions=acts)]
    await run(eng, p, [80, 80, 30, 30, 80])
    assert calls == ["turn_on", "turn_off", "turn_on"]


async def test_window_entry_keeps_running_when_its_gate_turns_false():
    """Unchanged: only Always entries treat the gate as the window."""
    h, p = FakeHandler(), {"soc": 80}
    eng = make_engine([entry(entry_conditions={"conditions": [GT]})], h, points=p)
    await run(eng, p, [80, 30, 30])
    assert h.state.active is True and len(forces(h)) == 1


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "always.db")
    yield conn
    await conn.close()


async def test_disable_and_enable_clears_the_done_mark(db):
    e = await create_schedule(db, "w", {"windows": [{"start": "10:00", "end": "11:00"}]},
                              "force_charge", {"power_w": 1000}, target_id="default")
    h = FakeHandler()
    t = [MON]
    eng = ScheduleEngine(db=db, resolver=lambda tt, tid: [("default", h)],
                         now_fn=lambda: t[0])
    await eng.reload()
    await eng.stop_entry(e["id"], now=MON)
    t[0] = MON + timedelta(minutes=1)
    await eng.reload()
    assert len(forces(h)) == 1
    await update_schedule(db, e["id"], enabled=False)
    await eng.reload()
    await update_schedule(db, e["id"], enabled=True)
    t[0] = MON + timedelta(minutes=2)
    await eng.reload()
    assert len(forces(h)) == 2
