"""Scheduler debug monitor: one in-memory line per enabled entry per tick,
saying where the entry stands and why. Reuses the engine-test fakes."""

from __future__ import annotations

from datetime import datetime, timedelta

from franklinwh_bridge.gateway.scheduler import MONITOR_MAX
from tests.unit.test_scheduler_engine import GT, LE20, MON, FakeHandler, entry, make_engine


def states(eng, entry_id=None):
    return [m["state"] for m in eng.monitor(entry_id)]


async def test_idle_outside_window():
    eng = make_engine([entry()], FakeHandler(), points={"soc": 80})
    await eng.tick(datetime(2026, 6, 15, 12, 0))
    line = eng.monitor()[-1]
    assert line["state"] == "idle"
    assert line["entry_id"] == "e1" and line["target"] == "default"


async def test_blocked_names_failing_condition_and_live_value():
    e = entry(entry_conditions={"conditions": [GT]})
    eng = make_engine([e], FakeHandler(), points={"soc": 30})
    await eng.tick(MON)
    await eng.tick(MON)
    lines = eng.monitor()
    # One line per tick, not only on a change (the Activity Log audits once).
    assert [m["state"] for m in lines] == ["blocked", "blocked"]
    assert "battery.soc_pct" in lines[-1]["detail"] and "live=30" in lines[-1]["detail"]
    c = lines[-1]["conditions"][0]
    assert c["kind"] == "entry" and c["live_value"] == 30 and c["result"] is False


async def test_running_shows_exit_not_met_then_done_after_exit():
    pts = {"soc": 50}
    eng = make_engine([entry(exit_conditions={"conditions": [LE20]})], FakeHandler(), points=pts)
    await eng.tick(MON)
    line = eng.monitor()[-1]
    assert line["state"] == "running"
    assert "exit not met" in line["detail"]
    assert line["conditions"][0]["kind"] == "exit"
    pts["soc"] = 10
    await eng.tick(MON)
    assert eng.monitor()[-1]["state"] == "done"


async def test_holding_counts_down():
    e = entry(entry_conditions={"conditions": [GT]}, entry_hold_s=60)
    eng = make_engine([e], FakeHandler(), points={"soc": 80})
    await eng.tick(MON)
    await eng.tick(MON + timedelta(seconds=15))
    line = eng.monitor()[-1]
    assert line["state"] == "holding"
    assert "45s left of 60s" in line["detail"]


async def test_skipped_names_the_entry_holding_the_gateway():
    a = entry(id="a", name="Top", priority=0)
    b = entry(id="b", name="Second", priority=1)
    eng = make_engine([a, b], FakeHandler(), points={"soc": 80})
    await eng.tick(MON)
    assert states(eng, "a") == ["running"]
    line = eng.monitor("b")[-1]
    assert line["state"] == "skipped" and "'Top'" in line["detail"]


async def test_loser_with_failing_gate_reads_blocked_not_skipped():
    a = entry(id="a", name="Top", priority=0)
    b = entry(id="b", name="Second", priority=1, entry_conditions={"conditions": [GT]})
    eng = make_engine([a, b], FakeHandler(), points={"soc": 30})
    await eng.tick(MON)
    line = eng.monitor("b")[-1]
    assert line["state"] == "blocked"
    assert "live=30" in line["detail"] and "'Top'" in line["detail"]


async def test_deferred_under_manual_control():
    h = FakeHandler()
    h.state.active, h.state.action = True, "Force Discharge"
    eng = make_engine([entry()], h, points={"soc": 80})
    await eng.tick(MON)
    assert states(eng) == ["deferred"]


async def test_since_returns_only_new_lines_and_buffer_is_capped():
    eng = make_engine([entry()], FakeHandler(), points={"soc": 80})
    await eng.tick(MON)
    last = eng.monitor()[-1]["seq"]
    await eng.tick(MON)
    assert [m["seq"] for m in eng.monitor(since=last)] == [last + 1]
    for _ in range(MONITOR_MAX + 5):
        await eng.tick(datetime(2026, 6, 15, 12, 0))
    assert len(eng.monitor()) == MONITOR_MAX


async def test_disabled_entries_are_not_monitored():
    eng = make_engine([], FakeHandler(), points={"soc": 80})
    await eng.tick(MON)
    assert eng.monitor() == []
