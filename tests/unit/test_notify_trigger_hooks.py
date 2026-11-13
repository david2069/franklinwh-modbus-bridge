"""Where the Auto-notify triggers fire: aGate offline / online from the alert monitor,
battery dispatch started / ended from the command handler, and the Scheduler group from
the schedule engine."""

from __future__ import annotations

import asyncio

from franklinwh_bridge.alert_monitor import AlertMonitor
from franklinwh_bridge.publish.command_handler import CommandHandler
from tests.conftest import calibrated_rating

from .test_alert_triggers import T0, _gateway, _run, db, reg  # noqa: F401
from .test_dispatch_verification import _Sample, controller
from .test_scheduler_engine import _SINCE, MON, FakeHandler, entry, make_engine


class _Calls(list):
    async def __call__(self, *args):
        self.append(args)


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_agate_offline_after_5_min_then_online(db, reg):  # noqa: F811
    told = _Calls()
    gw = _gateway(health="unreachable")
    mon = AlertMonitor(db, type("R", (), {"instances": {"gw1": gw}})(), worker_registry=reg,
                       on_reachability=told)

    def fresh(t):
        gw.poller.state.last_good_ts = t

    await _run(mon, T0, T0 + 300, before=fresh)
    await _settle()
    assert told == [("gw1", False)]
    gw.status.health = "connected"
    await _run(mon, T0 + 315, T0 + 435, before=fresh)
    await _settle()
    assert told == [("gw1", False), ("gw1", True)]


async def test_dispatch_started_once_then_ended(db):  # noqa: F811
    told = _Calls()
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    h.on_dispatch = told
    await h.handle_command("battery_command_power_pct", "50")
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    await h.handle_command("battery_command", "Release", requester="user:admin")
    await _settle()
    assert [c[0] for c in told] == ["dispatch_start", "dispatch_end"]
    assert told[0][1]["source"] == "a schedule"
    assert told[0][1]["metrics"].startswith("Force Discharge 50%")
    assert told[1][1]["detail"].startswith("Force Discharge; release; Released")


async def test_external_dispatch_started_and_ended(db):  # noqa: F811
    told = _Calls()
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    h.on_dispatch = told
    t = 1_000_000.0
    await h.observe_sample(_Sample(t, wset_enabled=0, wset_pct=0))
    await h.observe_sample(_Sample(t + 10, wset_enabled=1, wset_pct=30.0, battery_power_w=1500))
    await h.observe_sample(_Sample(t + 70, wset_enabled=0, wset_pct=0))
    await _settle()
    assert [c[0] for c in told] == ["dispatch_start", "dispatch_end"]
    assert told[0][1]["source"].startswith("external")
    assert told[1][1] == {"detail": "external, after 1 min"}


async def test_a_release_with_nothing_running_is_not_an_end(db):  # noqa: F811
    told = _Calls()
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    h.on_dispatch = told
    await h.handle_command("battery_command", "Release")
    await _settle()
    assert told == []


async def test_missed_run_is_a_schedule_missed_notification():
    told = _Calls()
    e = entry(trigger_kind="oneoff", trigger_spec={"fire_at": "2026-06-15T09:30:00"},
              duration_s=1800, when_spec={}, name="Evening peak")
    eng = make_engine([e], FakeHandler(), audits=[])
    eng.on_outcome = told
    await eng.catchup("default", _SINCE, now=MON)
    await _settle()
    [(event, name, gw, detail)] = told
    assert (event, name, gw) == ("schedule_missed", "Evening peak", "default")
    assert "missed during outage" in detail


async def test_schedule_failed_alert_is_a_schedule_failed_notification():
    told = _Calls()
    eng = make_engine([], FakeHandler())
    eng.on_outcome = told
    await eng._alert("SCHEDULE_FAILED", {"id": "e1", "name": "Peak"}, ("gateway", "", "gw1"),
                     "no successful dispatch")
    await eng._alert("COMMAND_UNCONFIRMED", {"id": "e1"}, ("gateway", "", "gw1"), "x")
    await _settle()
    assert told == [("schedule_failed", "Peak", "gw1", "no successful dispatch")]
