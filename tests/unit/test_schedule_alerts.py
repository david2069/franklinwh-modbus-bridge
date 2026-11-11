"""Scheduler failures raise bridge alerts (SCHEDULE_FAILED, COMMAND_UNCONFIRMED), so they
reach the alert modal and Home Assistant even when the entry has no notify target.
Both codes were in the catalogue but nothing raised them."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from franklinwh_bridge.gateway import scheduler as sched
from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.store.db import init_db
from tests.unit.test_dispatch_verification import T0, VHandler, entry, pts


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "alerts.db")
    yield conn
    await conn.close()


def make(db, entries, handler, points, ha_action_fn=None, ha_notify_fn=None):
    async def on_audit(*_):
        return None

    eng = ScheduleEngine(
        db=db, resolver=lambda tt, tid: [("default", handler)], on_audit=on_audit,
        points_fn=lambda gw: points, ha_action_fn=ha_action_fn, ha_notify_fn=ha_notify_fn,
    )
    eng._entries = entries
    return eng


async def alerts(db) -> list[tuple]:
    async with db.execute("SELECT code, severity, gateway_id, message FROM alerts") as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_not_effective_raises_command_unconfirmed_without_a_notify_target(db):
    h = VHandler()
    p = pts(T0, battery_power_w=0)
    eng = make(db, [entry(ha_actions=[])], h, p)
    await eng.tick(T0)
    t = T0
    for _ in range(2):  # judged, re-sent once, judged again
        t += timedelta(seconds=sched.VERIFY_AFTER_S + 1)
        p.update(pts(t, battery_power_w=0))
        await eng.tick(t)
    got = await alerts(db)
    assert [(c, s, g) for c, s, g, _ in got] == [("COMMAND_UNCONFIRMED", "critical", "default")]
    assert "Battery Bonus TOU Export" in got[0][3] and "not effective" in got[0][3]


async def test_moving_battery_raises_no_alert(db):
    h = VHandler()
    p = pts(T0)
    eng = make(db, [entry(ha_actions=[])], h, p)
    await eng.tick(T0)
    t = T0 + timedelta(seconds=sched.VERIFY_AFTER_S + 1)
    p.update(pts(t, battery_power_w=950))
    await eng.tick(t)
    assert await alerts(db) == []


async def test_window_ending_without_a_dispatch_raises_schedule_failed(db):
    h = VHandler(outcomes=[False] * 1000)
    eng = make(db, [entry(ha_actions=[])], h, pts(T0))
    t = T0
    while t < datetime(2026, 6, 15, 19, 30):
        await eng.tick(t)
        t += timedelta(seconds=15)
    assert await alerts(db) == []  # failed sends are COMMAND_REFUSED, raised by the handler
    await eng.tick(datetime(2026, 6, 15, 19, 31))
    got = await alerts(db)
    assert [c for c, *_ in got] == ["SCHEDULE_FAILED"]
    assert "window ended without a successful dispatch" in got[0][3]


async def test_lost_setpoint_cap_raises_schedule_failed(db):
    h = VHandler()
    p = pts(T0)
    eng = make(db, [entry(ha_actions=[])], h, p)
    await eng.tick(T0)
    t = T0
    for _ in range(sched.MAX_REASSERTS + 1):
        t += timedelta(seconds=15)
        p.update(pts(t, wset_enabled=0))
        await eng.tick(t)
    got = await alerts(db)
    assert [c for c, *_ in got] == ["SCHEDULE_FAILED"] and "setpoint lost" in got[0][3]


async def test_failed_ha_entity_action_raises_schedule_failed_but_failed_notify_does_not(db):
    action = {"kind": "action", "instance_id": "ha1", "entity_id": "switch.pool",
              "service": "turn_off", "when": "fire"}
    notify = {"kind": "notify", "instance_id": "ha1", "service": "mobile_app_phone",
              "message": "hi", "when": "fire"}

    async def fail_action(inst, eid, svc, data):
        return {"ok": False, "error": "HA unreachable"}

    async def fail_notify(inst, svc, title, msg):
        return {"ok": False, "error": "HA unreachable"}

    eng = make(db, [entry(ha_actions=[action, notify])], VHandler(), pts(T0),
               ha_action_fn=fail_action, ha_notify_fn=fail_notify)
    await eng.tick(T0)
    got = await alerts(db)
    assert [c for c, *_ in got] == ["SCHEDULE_FAILED"]
    assert "switch.pool" in got[0][3] and "HA unreachable" in got[0][3]
