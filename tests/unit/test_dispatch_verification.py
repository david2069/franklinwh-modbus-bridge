"""Dispatch verification (design PR #57 §5.1, tests T1–T7 against fakes).

Replays the three silent failures:
- 2026-10-12 (#56): the send timed out and was audited "ok", with no retry.
- 2026-10-07 (#26): the setpoint was accepted, but the battery stayed at 0 W.
- 2026-10-04 (#25): stale data must not drive a decision.

Scheduler tests use a fake handler and injected points. Command handler tests
use a MagicMock controller and a real DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.gateway import scheduler as sched
from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.publish.command_handler import CommandHandler, DispatchResult
from franklinwh_bridge.store.db import init_db
from tests.conftest import calibrated_rating

T0 = datetime(2026, 6, 15, 18, 0, 5)  # inside the 18:00–19:30 window, a Monday
NOTIFY = {"kind": "notify", "instance_id": "ha1", "service": "mobile_app_phone",
          "message": "SoC %battery.soc_pct%", "when": "fire"}


def entry(**kw):
    base = {
        "id": "e1", "name": "Battery Bonus TOU Export", "enabled": True,
        "when_spec": {"windows": [{"start": "18:00", "end": "19:30"}]},
        "action": "force_discharge", "params": {"power_w": 1000},
        "target_type": "gateway", "target_id": "default",
        "release": "release", "conflict": "defer", "priority": 0,
        "entry_conditions": None, "exit_conditions": None,
        "ha_actions": [NOTIFY],
    }
    base.update(kw)
    return base


class VHandler:
    """Fake CommandHandler that reports dispatch results like the real one."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])  # per battery send: True ok / False fail
        self.sends: list[tuple[str, str | None]] = []
        self.last_dispatch: DispatchResult | None = None
        self.active_dispatch_id: str | None = None
        self._n = 0

        class _S:
            active = False
            action = ""
            last_success = True

        self.state = _S()

    async def handle_command(self, slug, value, requester=None):
        if slug != "battery_command":
            return
        if value in ("Release", "Not Active", "Stop"):
            self.state.active = False
            self.state.action = ""
            return
        self.sends.append((value, requester))
        ok = self.outcomes.pop(0) if self.outcomes else True
        base = value[6:] if value.lower().startswith("force ") else value
        watts = -1000 if base == "Discharge" else 1000 if base == "Charge" else 0
        self._n += 1
        if ok:
            self.state.active = True
            self.state.action = f"Force {base}"
            self.active_dispatch_id = f"d{self._n}"
            self.last_dispatch = DispatchResult(True, "OK", self.state.action, watts,
                                                dispatch_id=self.active_dispatch_id)
        else:
            self.state.active = False
            self.state.action = ""
            self.last_dispatch = DispatchResult(False, "Response timeout", f"Force {base}",
                                                watts, dispatch_id=f"d{self._n}")


def make(entries, handler, points):
    audits, notes = [], []

    async def on_audit(sid, action, target, result, detail):
        audits.append({"id": sid, "action": action, "result": result, "detail": detail})

    async def notify(inst, svc, title, msg):
        notes.append((inst, svc, msg))
        return {"ok": True}

    eng = ScheduleEngine(
        db=None, resolver=lambda tt, tid: [("default", handler)],
        on_audit=on_audit, points_fn=lambda gw: points, ha_notify_fn=notify,
    )
    eng._entries = entries
    return eng, audits, notes


def pts(now, **kw):
    """Fresh, good-quality points as of ``now``."""
    base = {"_sample_ts": now.timestamp(), "_sample_quality": "ok", "soc": 80,
            "active_reserve_pct": 5, "wset_enabled": 1, "battery_power_w": 0}
    base.update(kw)
    return base


def results(audits, kind):
    return [a for a in audits if a["result"] == kind]


# ── T1: 10-12 replay — the first send fails ─────────────────────────────


async def test_t1_failed_send_is_failed_not_ok_and_retried_with_backoff():
    h = VHandler(outcomes=[False, True])
    p = pts(T0)
    eng, audits, notes = make([entry()], h, p)

    await eng.tick(T0)
    assert results(audits, "failed") and not results(audits, "ok")
    assert "Response timeout" in results(audits, "failed")[0]["detail"]
    assert h.state.active is False
    assert len(h.sends) == 1
    assert h.sends[0][1] == "schedule:e1"  # the requester reaches the handler

    await eng.tick(T0 + timedelta(seconds=10))  # inside the 15 s backoff
    assert len(h.sends) == 1

    await eng.tick(T0 + timedelta(seconds=16))
    assert len(h.sends) == 2
    ok = results(audits, "ok")
    assert ok and "recovered after 1 failed attempt" in ok[0]["detail"]
    assert h.state.active is True
    # Owner told about the failure, then the recovery, on the entry's own target.
    assert [n[0:2] for n in notes].count(("ha1", "mobile_app_phone")) >= 2
    assert any("FAILED" in n[2] for n in notes) and any("Recovered" in n[2] for n in notes)


async def test_t6_every_attempt_fails_bounded_and_one_final_failure_at_window_end():
    h = VHandler(outcomes=[False] * 1000)
    eng, audits, notes = make([entry()], h, pts(T0))
    t = T0
    while t < datetime(2026, 6, 15, 19, 30):
        await eng.tick(t)
        t += timedelta(seconds=15)
    # Backoff 15, 30, then 60 s: about 90 attempts in 90 min, not one per tick.
    assert 85 <= len(h.sends) <= 95
    await eng.tick(datetime(2026, 6, 15, 19, 31))  # window over
    final = results(audits, "failed")[-1]
    assert "window ended without a successful dispatch" in final["detail"]
    alerts = [n for n in notes if "FAILED" in n[2]]
    assert len(alerts) == 2  # the first failure + the final one, no spam


# ── T2: 10-07 replay — accepted but the battery doesn't move ────────────


async def test_t2_not_effective_resends_once_then_notifies():
    h = VHandler()
    p = pts(T0, battery_power_w=0)
    eng, audits, notes = make([entry()], h, p)
    await eng.tick(T0)
    assert len(h.sends) == 1

    p.update(pts(T0 + timedelta(seconds=30), battery_power_w=0))
    await eng.tick(T0 + timedelta(seconds=30))  # before VERIFY_AFTER_S: no judgement
    assert not results(audits, "not_effective")

    t1 = T0 + timedelta(seconds=sched.VERIFY_AFTER_S + 1)
    p.update(pts(t1, battery_power_w=0))
    await eng.tick(t1)
    assert len(results(audits, "not_effective")) == 1
    assert len(h.sends) == 2  # re-sent once

    t2 = t1 + timedelta(seconds=sched.VERIFY_AFTER_S + 1)
    p.update(pts(t2, battery_power_w=0))
    await eng.tick(t2)
    assert len(results(audits, "not_effective")) == 2
    assert len(h.sends) == 2  # no further re-sends
    assert any("NOT EFFECTIVE" in n[2] for n in notes)

    t3 = t2 + timedelta(seconds=sched.VERIFY_AFTER_S + 1)
    p.update(pts(t3, battery_power_w=0))
    await eng.tick(t3)
    assert len(h.sends) == 2 and len(results(audits, "not_effective")) == 2


async def test_t2_moving_battery_is_verified_once_with_no_extra_writes():
    h = VHandler()
    p = pts(T0)
    eng, audits, _ = make([entry()], h, p)
    await eng.tick(T0)
    for k in (1, 2, 3):
        t = T0 + timedelta(seconds=k * (sched.VERIFY_AFTER_S + 1))
        p.update(pts(t, battery_power_w=950))  # + = discharging
        await eng.tick(t)
    assert len(results(audits, "verified")) == 1
    assert len(h.sends) == 1  # T5: nothing re-written while output is correct


async def test_t7_soc_limit_is_not_judged_as_not_effective():
    h = VHandler()
    p = pts(T0, soc=7, active_reserve_pct=5, battery_power_w=0)
    eng, audits, notes = make([entry()], h, p)
    await eng.tick(T0)
    t = T0 + timedelta(seconds=sched.VERIFY_AFTER_S + 1)
    p.update(pts(t, soc=7, active_reserve_pct=5, battery_power_w=0))
    await eng.tick(t)
    assert results(audits, "unverifiable") and not results(audits, "not_effective")
    assert len(h.sends) == 1 and not notes[1:]


# ── T3: 10-04 replay — stale data never drives a decision ───────────────


async def test_t3_stale_points_do_not_trigger_verification_or_reassert():
    h = VHandler()
    p = pts(T0)
    eng, audits, _ = make([entry()], h, p)
    await eng.tick(T0)
    t = T0 + timedelta(seconds=sched.VERIFY_AFTER_S + 1)
    stale = t - timedelta(seconds=sched.POINTS_MAX_AGE_S + 5)
    p.update(pts(stale, battery_power_w=0, wset_enabled=0))
    await eng.tick(t)
    assert not results(audits, "not_effective") and not results(audits, "reasserted")
    assert len(h.sends) == 1


# ── T4: the aGate drops the setpoint mid-window ─────────────────────────


async def test_t4_lost_setpoint_is_reasserted_then_capped():
    h = VHandler()
    p = pts(T0)
    eng, audits, notes = make([entry()], h, p)
    await eng.tick(T0)
    t = T0
    for _ in range(sched.MAX_REASSERTS):
        t += timedelta(seconds=15)
        p.update(pts(t, wset_enabled=0))
        await eng.tick(t)
    assert len(results(audits, "reasserted")) == sched.MAX_REASSERTS
    assert len(h.sends) == 1 + sched.MAX_REASSERTS
    t += timedelta(seconds=15)
    p.update(pts(t, wset_enabled=0))
    await eng.tick(t)
    assert len(h.sends) == 1 + sched.MAX_REASSERTS  # capped
    assert "setpoint lost" in results(audits, "failed")[-1]["detail"]
    assert any("setpoint lost" in n[2] for n in notes)


async def test_no_notify_target_is_audited_not_silent():
    h = VHandler(outcomes=[False])
    eng, audits, _ = make([entry(ha_actions=[])], h, pts(T0))
    await eng.tick(T0)
    assert any(a["action"] == "notify" and a["result"] == "skipped" for a in audits)


# ── Command handler: honest results (design P2, P4) ─────────────────────


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "d.db")
    yield conn
    await conn.close()


def controller(send=(True, "Command Sent"), ena_after_send=1, ena_after_release=0):
    c = MagicMock()
    c.send_command.return_value = send
    c.reset_control_state.return_value = True
    c.get_model.return_value = None
    state = {"ena": 0}

    def _send(*a, **k):
        state["ena"] = ena_after_send
        return send

    def _release(*a, **k):
        state["ena"] = ena_after_release
        return True

    c.send_command.side_effect = _send
    c.reset_control_state.side_effect = _release
    c.read_control_status.side_effect = lambda: {"wset_enabled": state["ena"], "wset_pct": 0}
    return c


async def _rows(db, sql):
    async with db.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_handler_failed_send_is_not_active_and_recorded(db):
    h = CommandHandler(controller(send=(False, "Response timeout"), ena_after_send=0), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    assert h.state.active is False
    assert h.last_dispatch.ok is False and "Response timeout" in h.last_dispatch.reason
    assert ("command_failed",) in await _rows(db, "SELECT event FROM control_log")
    assert await _rows(db, "SELECT state, requester FROM dispatch_outcomes") == [
        ("failed", "schedule:e1")]


async def test_handler_accepted_but_not_enabled_is_failed(db):
    h = CommandHandler(controller(send=(True, "Command Sent"), ena_after_send=0), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")
    assert h.state.active is False
    assert "not enabled after send" in h.last_dispatch.reason


async def test_handler_success_then_release_is_recorded(db):
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")
    assert h.state.active is True and h.last_dispatch.ok is True
    await h.handle_command("battery_command", "Release")
    assert await _rows(db, "SELECT state, requester FROM dispatch_outcomes") == [
        ("released", "manual")]


async def test_handler_release_that_leaves_wset_enabled_is_not_verified(db):
    h = CommandHandler(controller(ena_after_release=1), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")
    await h.handle_command("battery_command", "Release")
    assert h.state.last_success is False
    assert "not verified" in h.state.last_result
    assert await _rows(db, "SELECT state FROM dispatch_outcomes") == [("release_unverified",)]


async def test_handler_new_command_supersedes_the_active_row(db):
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")
    await h.handle_command("battery_command", "Force Charge")
    states = await _rows(db, "SELECT action, state FROM dispatch_outcomes ORDER BY created_at")
    assert states == [("Force Discharge", "superseded"), ("Force Charge", "sent")]



async def test_dispatch_outcomes_endpoint(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from franklinwh_bridge.api.gateways_api import router

    h = CommandHandler(controller(send=(False, "Response timeout"), ena_after_send=0), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    app = FastAPI()
    app.state.db = db
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/gateways/default/dispatch-outcomes")
    assert r.status_code == 200
    rows = r.json()["outcomes"]
    assert [(o["state"], o["requester"], o["requested_w"]) for o in rows] == [
        ("failed", "schedule:e1", -5000)]


# ── Who sent it, and dispatches this bridge did not send ────────────────


async def test_command_records_who_sent_it(db):
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command_target_soc", "60", requester="user:admin")
    await h.handle_command("battery_command", "Force Discharge", requester="user:admin")
    await h.handle_command("battery_command", "Release", requester="mqtt")
    rows = await _rows(db, "SELECT event, detail FROM control_log ORDER BY id")
    assert rows[0][0] == "target_soc_set" and rows[0][1].endswith("by user:admin")
    assert rows[1][0] == "command_sent" and rows[1][1].endswith("by user:admin")
    assert rows[2][0] == "command_released" and rows[2][1].endswith("by mqtt")
    assert await _rows(db, "SELECT requester FROM dispatch_outcomes") == [("user:admin",)]


class _Sample:
    def __init__(self, ts, **points):
        self.ts, self.points = ts, points


async def test_external_dispatch_is_logged_start_and_end(db):
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    t = 1_000_000.0
    await h.observe_sample(_Sample(t, wset_enabled=0, wset_pct=0))
    await h.observe_sample(_Sample(t + 10, wset_enabled=1, wset_pct=30.0, battery_power_w=1500))
    await h.observe_sample(_Sample(t + 20, wset_enabled=1, wset_pct=30.0, battery_power_w=1500))
    await h.observe_sample(_Sample(t + 70, wset_enabled=0, wset_pct=0))
    rows = await _rows(db, "SELECT event, power_w, detail FROM control_log ORDER BY id")
    assert [r[0] for r in rows] == ["external_dispatch_started", "external_dispatch_ended"]
    assert rows[0][1] == 1500 and "not sent by this bridge" in rows[0][2]
    assert "after 60s" in rows[1][2]


async def test_own_dispatch_is_not_reported_as_external(db):
    import time as _t
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge", requester="user:admin")
    now = _t.time()
    await h.observe_sample(_Sample(now + 5, wset_enabled=1, wset_pct=100.0))   # settling
    await h.observe_sample(_Sample(now + 60, wset_enabled=1, wset_pct=100.0))  # ours, matches
    events = [r[0] for r in await _rows(db, "SELECT event FROM control_log")]
    assert not any(e.startswith("external") for e in events)


async def test_setpoint_changed_underneath_ours_is_logged_once(db):
    import time as _t
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")  # 100% of 5000 W
    now = _t.time()
    for k in (60, 70, 80):
        await h.observe_sample(_Sample(now + k, wset_enabled=1, wset_pct=30.0))
    events = [r[0] for r in await _rows(db, "SELECT event FROM control_log")]
    assert events.count("external_change") == 1


async def test_sequencer_needs_control_and_records_who_ran_it(tmp_path, monkeypatch):
    import asyncio
    import sys
    import types
    from types import SimpleNamespace

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.middleware.sessions import SessionMiddleware

    from franklinwh_bridge.api.admin import router as admin_router
    from franklinwh_bridge.api.auth import router as auth_router
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user

    class FakeSeq:  # never touches a register
        def __init__(self, dev):
            self.verbose = False

        def run_sequence(self, steps, dry_run=False):
            return True

    monkeypatch.setitem(sys.modules, "franklinwh_modbus.sequencer",
                        types.SimpleNamespace(SunSpecSequencer=FakeSeq))
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    conn = await init_db(tmp_path / "s.db")
    await create_user(conn, "admin", hash_password("pw"), role="admin")
    await create_user(conn, "viewer1", hash_password("pw"), role="viewer")
    inst = SimpleNamespace(config=SimpleNamespace(mock=False),
                           controller=SimpleNamespace(dev=object()),
                           modbus_lock=asyncio.Lock())
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = conn
    app.state.config = SimpleNamespace(environment="docker")
    app.state.registry = SimpleNamespace(get=lambda gid: inst, list_active=lambda: ["default"])
    app.include_router(auth_router)
    app.include_router(admin_router)
    body = {"inline": '{"DERCtlAC.WSetEna": 1, "DERCtlAC.WSetPct": 30}'}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as v:
        await v.post("/api/auth/login", json={"username": "viewer1", "password": "pw"})
        assert (await v.post("/api/sequence/execute", json=body)).status_code == 403
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as a:
        await a.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        r = await a.post("/api/sequence/execute", json=body)
        assert r.status_code == 200 and r.json()["ok"] is True
    rows = await _rows(conn, "SELECT event, action, detail, gateway_id FROM control_log")
    assert rows == [("sequence_executed", "sequencer",
                     "DERCtlAC.WSetEna=1, DERCtlAC.WSetPct=30 — by user:admin", "default")]
    await conn.close()
