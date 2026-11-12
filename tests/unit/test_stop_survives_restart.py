"""Stop, and an exit inside a window, hold until that window ends, including across a
bridge restart (#231). Before the fix the done mark lived only in memory, so the first
tick after a restart fired the stopped window again."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.store.db import create_schedule, init_db
from tests.unit.test_dispatch_verification import VHandler, pts

WIN = {"windows": [{"start": "18:00", "end": "20:00"}]}
D = datetime(2026, 6, 15)  # a Monday


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "done.db")
    yield conn
    await conn.close()


async def boot(db, handler, now):
    """A freshly started engine, as after a restart (the loop is not left running)."""
    eng = ScheduleEngine(db=db, resolver=lambda tt, tid: [("default", handler)],
                         now_fn=lambda: now, points_fn=lambda gw: pts(now))
    await eng.start()
    await eng.stop()
    return eng


def forces(h):
    return [v for v, _by in h.sends if v not in ("Release", "Not Active", "Stop")]


async def discharge(db, **kw):
    return await create_schedule(db, "Evening export", kw.pop("when_spec", WIN),
                                 "force_discharge", {"power_w": 1000},
                                 target_id="default", **kw)


async def test_stop_is_not_undone_by_a_restart_in_the_same_window(db):
    e = await discharge(db)
    h = VHandler()
    eng = await boot(db, h, D.replace(hour=18))
    await eng.tick(D.replace(hour=18, minute=1))
    assert len(forces(h)) == 1
    await eng.stop_entry(e["id"], now=D.replace(hour=18, minute=30))

    h2 = VHandler()  # restart at 18:45
    eng2 = await boot(db, h2, D.replace(hour=18, minute=45))
    for m in (46, 50, 59):
        await eng2.tick(D.replace(hour=18, minute=m))
    assert forces(h2) == []


async def test_next_window_fires_after_a_restart(db):
    e = await discharge(db)
    h = VHandler()
    eng = await boot(db, h, D.replace(hour=18))
    await eng.tick(D.replace(hour=18, minute=1))
    await eng.stop_entry(e["id"], now=D.replace(hour=18, minute=30))

    nxt = D + timedelta(days=1, hours=18, minutes=5)  # bridge was down overnight
    h2 = VHandler()
    eng2 = await boot(db, h2, nxt)
    await eng2.tick(nxt)
    assert len(forces(h2)) == 1
    async with db.execute("SELECT COUNT(*) FROM schedule_done") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_release_inside_the_window_is_not_undone_by_a_restart(db):
    await discharge(db)
    h = VHandler()
    eng = await boot(db, h, D.replace(hour=18))
    await eng.tick(D.replace(hour=18, minute=1))
    h.state.active = False  # released outside the scheduler (watchdog, top bar)
    await eng.tick(D.replace(hour=18, minute=2))

    h2 = VHandler()
    eng2 = await boot(db, h2, D.replace(hour=18, minute=45))
    await eng2.tick(D.replace(hour=18, minute=46))
    assert forces(h2) == []


async def test_stop_in_a_window_past_midnight_holds_across_a_restart(db):
    e = await discharge(db, when_spec={"windows": [{"start": "23:00", "end": "02:00"}]})
    h = VHandler()
    t = D.replace(hour=23, minute=30)
    eng = await boot(db, h, t)
    await eng.tick(t)
    await eng.stop_entry(e["id"], now=t + timedelta(minutes=1))

    t2 = D + timedelta(days=1, hours=1)
    h2 = VHandler()
    eng2 = await boot(db, h2, t2)
    await eng2.tick(t2)
    assert forces(h2) == []


def test_occurrence_is_the_start_of_the_open_window_or_fire():
    from franklinwh_bridge.gateway.scheduler import occurrence_start  # new in this fix

    over = {"when_spec": {"windows": [{"start": "23:00", "end": "02:00"}]}}
    assert occurrence_start(over, D.replace(hour=1)) == "2026-06-14T23:00:00"
    assert occurrence_start(over, D.replace(hour=23, minute=5)) == "2026-06-15T23:00:00"
    assert occurrence_start(over, D.replace(hour=12)) is None
    daily = {"trigger_kind": "daily", "trigger_spec": {"time_of_day": "18:00"},
             "duration_s": 3600}
    assert occurrence_start(daily, D.replace(hour=18, minute=30)) == "2026-06-15T18:00:00"
    assert occurrence_start(daily, D.replace(hour=19, minute=30)) is None
    assert occurrence_start({"trigger_kind": "always"}, D) is None
