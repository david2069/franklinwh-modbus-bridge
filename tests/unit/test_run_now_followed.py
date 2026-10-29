"""A Run now is followed like a scheduled run, and the engine's own releases
say which entry sent them.

On HA Live (29/10) a Run now of a Force Discharge entry outside its window was
fire-and-forget: the Monitor said IDLE, the entry's exit conditions were never
checked, nothing verified the battery moved, and only the aGate's 2-hour
timeout would have ended it. The 19:03 window-end release on 28/10 was logged
as sent by "manual" instead of the entry.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.store.db import create_schedule, init_db

OUT = datetime(2026, 6, 15, 12, 0)   # outside the 18:00–19:00 window, a Monday
IN = datetime(2026, 6, 15, 18, 0, 5)
WINDOW = {"windows": [{"start": "18:00", "end": "19:00"}]}
LT20 = {"conditions": [{"sensor": "battery.soc_pct", "op": "<", "value": 20}]}


class Handler:
    """Fake CommandHandler that records who sent each command."""

    def __init__(self):
        self.calls: list[tuple[str, str, str | None]] = []
        self.active_requester: str | None = None
        self.last_dispatch = None

        class _S:
            active = False
            action = ""
            last_success = True

        self.state = _S()

    async def handle_command(self, slug, value, requester=None):
        self.calls.append((slug, value, requester))
        if slug != "battery_command":
            return
        if value in ("Release", "Not Active", "Stop"):
            self.state.active = False
            self.state.action = ""
            self.active_requester = None
        else:
            base = value[6:] if value.lower().startswith("force ") else value
            self.state.active = True
            self.state.action = f"Force {base}"
            self.active_requester = requester

    def releases(self):
        return [c for c in self.calls if c[:2] == ("battery_command", "Release")]


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "r.db")
    yield conn
    await conn.close()


async def setup(db, points, **kw):
    row = await create_schedule(db, name="Force Discharge", when_spec=WINDOW,
                                action="force_discharge", params={"power_w": 5000}, **kw)
    h = Handler()
    audits: list[dict] = []

    async def on_audit(sid, action, target, result, detail):
        audits.append({"id": sid, "result": result, "detail": detail})

    eng = ScheduleEngine(db, resolver=lambda tt, tid: [("default", h)],
                         on_audit=on_audit, points_fn=lambda gw: points)
    await eng.load()
    return eng, h, audits, row["id"]


def last_monitor(eng):
    return eng.monitor()[-1]


async def test_run_now_shows_running_in_the_monitor_outside_its_window(db):
    eng, h, audits, sid = await setup(db, {"soc": 80}, exit_conditions=LT20)
    assert (await eng.execute(sid, now=OUT))["status"] == "fired"
    await eng.tick(OUT)
    m = last_monitor(eng)
    assert m["state"] == "running"
    assert m["detail"].startswith("Run now — running Force Discharge — exit not met")


async def test_run_now_ends_on_its_exit_conditions(db):
    pts = {"soc": 80}
    eng, h, audits, sid = await setup(db, pts, exit_conditions=LT20)
    await eng.execute(sid, now=OUT)
    await eng.tick(OUT)
    assert h.state.active is True
    pts["soc"] = 15
    await eng.tick(OUT)
    assert h.state.active is False
    assert h.releases() == [("battery_command", "Release", f"schedule:{sid}")]
    assert audits[-1]["result"] == "exit_condition_met"
    assert audits[-1]["detail"].startswith("Run now: exit conditions met")
    await eng.tick(OUT)
    assert last_monitor(eng)["state"] == "idle"


async def test_run_now_released_by_its_time_limit_is_logged_once(db):
    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.execute(sid, now=OUT)
    h.state.active = False  # the handler's watchdog released it
    await eng.tick(OUT)
    await eng.tick(OUT)
    ended = [a for a in audits if a["detail"].startswith("Run now ended")]
    assert [a["result"] for a in ended] == ["expired"]
    assert last_monitor(eng)["state"] == "idle"


async def test_run_now_replaced_by_another_command_ends(db):
    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.execute(sid, now=OUT)
    await h.handle_command("battery_command", "Force Charge", requester="user:david")
    await eng.tick(OUT)
    assert "replaced by another command (Force Charge)" in audits[-1]["detail"]
    assert h.releases() == []  # someone else's command is left alone


async def test_stop_releases_a_run_now(db):
    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.execute(sid, now=OUT)
    res = await eng.stop_entry(sid, now=OUT)
    assert res == {"status": "stopped", "released": ["default"]}
    assert h.releases() == [("battery_command", "Release", f"schedule:{sid}")]
    assert audits[-1]["result"] == "stopped"


async def test_a_user_release_ends_the_run_now_without_a_second_row(db):
    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.execute(sid, now=OUT)
    await h.handle_command("battery_command", "Release", requester="user:david")
    await eng.note_release("default", f"schedule:{sid}", "user:david", "Released")
    await eng.tick(OUT)
    assert [a["result"] for a in audits] == ["executed", "released"]


async def test_its_window_opening_takes_the_run_now_over(db):
    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.execute(sid, now=IN.replace(hour=17, minute=59))
    await eng.tick(IN)
    sends = [c for c in h.calls if c[1] == "Force Discharge"]
    assert len(sends) == 1  # not dispatched again
    assert last_monitor(eng)["detail"].startswith("running Force Discharge")
    await eng.tick(IN.replace(hour=19, minute=0, second=30))  # window over
    assert h.releases() == [("battery_command", "Release", f"schedule:{sid}")]


async def test_a_failed_run_now_is_audited_failed_not_executed(db):
    from franklinwh_bridge.publish.command_handler import DispatchResult

    eng, h, audits, sid = await setup(db, {"soc": 80})
    orig = h.handle_command

    async def failing(slug, value, requester=None):
        await orig(slug, value, requester)
        h.state.active = False
        h.last_dispatch = DispatchResult(False, "Response timeout", "Force Discharge", -5000)

    h.handle_command = failing
    res = await eng.execute(sid, now=OUT)
    assert res["status"] == "noop"
    assert audits[-1] == {"id": sid, "result": "failed",
                          "detail": "manual execute failed: Response timeout"}
    await eng.tick(OUT)
    assert last_monitor(eng)["state"] == "idle"


async def test_window_exit_release_names_the_entry(db):
    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.tick(IN)
    await eng.tick(IN.replace(hour=19, minute=0, second=30))
    assert h.releases() == [("battery_command", "Release", f"schedule:{sid}")]


async def test_the_entry_list_shows_a_running_run_now(db):
    from franklinwh_bridge.api.schedules_api import _decorate

    eng, h, audits, sid = await setup(db, {"soc": 80})
    await eng.execute(sid, now=OUT)
    entry = {"id": sid, "enabled": True, "when_spec": WINDOW}
    assert _decorate(entry, OUT, eng.run_now_ids)["run_now"] is True
    await eng.stop_entry(sid, now=OUT)
    assert _decorate(entry, OUT, eng.run_now_ids)["run_now"] is False
