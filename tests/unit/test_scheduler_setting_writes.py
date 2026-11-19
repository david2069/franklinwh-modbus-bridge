"""Mode and reserve entries are judged by the write's result (#166): the library reads the
register back, and a write the aGate accepted but didn't apply (extension registers on some
firmware) or that failed is audited 'failed', not 'ok — dispatched'."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.unit.test_scheduler_engine import MON, FakeHandler, entry, make_engine

IGNORED = ("Write failed (Read-Only?): Hardware ignored write to TOU. "
           "Register stayed at Self-Consumption.")


class SettingHandler(FakeHandler):
    """Applies mode and reserve writes, or answers each with the next queued failure."""

    def __init__(self, *fails: str):
        super().__init__()
        self.fails = list(fails)
        self.state.last_success, self.state.last_result = True, ""

    async def handle_command(self, slug: str, value: str) -> None:
        await super().handle_command(slug, value)
        if slug in ("operating_mode", "self_reserve_pct", "tou_reserve_pct"):
            why = self.fails.pop(0) if self.fails else ""
            self.state.last_success, self.state.last_result = not why, why or "ok"


def mode_entry(restore=False):
    return entry(action="mode", params={"mode": "TOU", **({"restore_mode": True} if restore
                                                          else {})})


def rows(audits, action):
    return [(a["result"], a["detail"]) for a in audits if a["action"] == action]


async def test_a_mode_write_the_gateway_ignored_is_failed_not_ok_and_not_retried():
    h, audits = SettingHandler(IGNORED), []
    eng = make_engine([mode_entry(restore=True)], h, points={"mode_name": "Self-Consumption"},
                      audits=audits)
    await eng.tick(MON)
    [(result, detail)] = rows(audits, "mode")
    assert result == "failed" and detail.startswith("not applied: ")
    assert "Hardware ignored write" in detail and "not retrying this window" in detail
    assert eng._mode_back == {}  # nothing changed, so nothing to put back
    h.calls.clear()
    await eng.tick(MON + timedelta(minutes=5))  # same window: not sent again
    assert h.calls == []
    await eng.tick(datetime(2026, 6, 15, 11, 5))  # window over: no "put back" note
    assert not [d for _, d in rows(audits, "mode") if "put back" in d]


async def test_a_reserve_write_that_failed_is_retried_and_recovers():
    h, audits = SettingHandler("Modbus timeout"), []
    e = entry(action="reserve_self", params={"pct": 30})
    eng = make_engine([e], h, points={}, audits=audits)
    await eng.tick(MON)
    assert rows(audits, "reserve_self") == [
        ("failed", "dispatch failed: Modbus timeout — attempt 1, retrying in 15s")]
    await eng.tick(MON + timedelta(seconds=20))
    assert h.calls.count(("self_reserve_pct", "30")) == 2
    result, detail = rows(audits, "reserve_self")[-1]
    assert result == "ok" and "recovered after 1 failed attempt(s)" in detail


async def test_run_now_reports_an_ignored_mode_write_as_failed(tmp_path):
    from franklinwh_bridge.gateway.scheduler import ScheduleEngine
    from franklinwh_bridge.store.db import create_schedule, init_db

    db = await init_db(tmp_path / "r.db")
    row = await create_schedule(db, name="Mode", when_spec=mode_entry()["when_spec"],
                                action="mode", params={"mode": "TOU"})
    h, audits = SettingHandler(IGNORED), []

    async def on_audit(sid, action, target, result, detail):
        audits.append({"action": action, "result": result, "detail": detail})

    eng = ScheduleEngine(db, resolver=lambda tt, tid: [("default", h)], on_audit=on_audit,
                         points_fn=lambda gw: {})
    await eng.load()
    out = await eng.execute(row["id"], now=MON)
    assert out["dispatched"] == []
    assert rows(audits, "mode") == [("failed", f"manual execute failed: {IGNORED}")]
    await db.close()


async def test_a_write_that_took_is_still_ok():
    h, audits = SettingHandler(), []
    eng = make_engine([mode_entry()], h, points={}, audits=audits)
    await eng.tick(MON)
    [(result, detail)] = rows(audits, "mode")
    assert result == "ok" and detail.startswith("dispatched")
