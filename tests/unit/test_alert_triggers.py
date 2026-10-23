"""#324: what raises alerts. Refused commands at the source; conditions from the monitor.

Shared spec with the DC bridge (its #271): GATEWAY_UNREACHABLE opens after
5 min bad and resolves after 2 min good; LIVE_DATA_MISSING and
DATA_MISSING_PARTIAL after 2 / 1 min; WORKER_STALLED when a worker hasn't
beaten for max(4 × cadence, 30 s), or its task died.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge import workers
from franklinwh_bridge.alert_monitor import AlertMonitor, Debounce
from franklinwh_bridge.api.health import health_workers
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store.db import init_db
from tests.conftest import calibrated_rating

T0 = 1_792_536_000.0


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "alerts.db")
    yield conn
    await conn.close()


async def _alerts(db, **kw):
    return await alert_store.list_alerts(db, **kw)


# ── Refused commands ────────────────────────────────────────────────


async def test_agate_rejection_raises_command_refused_and_merges(db):
    ctrl = MagicMock()
    ctrl.send_command = MagicMock(return_value=(False, "Modbus exception: 3"))
    ctrl.read_control_status = MagicMock(return_value={"wset_enabled": 0, "wset_pct": 0})
    h = CommandHandler(ctrl, db, modbus_lock=asyncio.Lock(), gateway_id="gw1",
                       rating=calibrated_rating("gw1"))
    await h.handle_command("battery_command_power_pct", "50")
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:s1")
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:s1")
    [a] = await _alerts(db)
    assert (a["code"], a["gateway_id"], a["severity"], a["count"]) == (
        "COMMAND_REFUSED", "gw1", "critical", 2)
    assert a["message"].startswith("Battery command Force Discharge (by schedule:s1): ")
    assert "Rejected by the aGate (Modbus exception 3" in a["message"]


async def test_bridge_refusal_says_so(db):
    h = CommandHandler(MagicMock(), db, modbus_lock=asyncio.Lock())
    await h.handle_command("battery_command", "Force Sideways")
    [a] = await _alerts(db)
    assert a["code"] == "COMMAND_REFUSED"
    assert "Unknown battery command: Force Sideways" in a["message"]


async def test_mode_write_kept_old_value_is_write_ignored(db):
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(False, "Hardware ignored write to 15508"))
    h = CommandHandler(ctrl, db, modbus_lock=asyncio.Lock())
    await h.handle_command("operating_mode", "TOU")
    [a] = await _alerts(db)
    assert a["code"] == "WRITE_IGNORED" and a["message"].startswith("Operating mode TOU")


async def test_success_staging_and_unknown_slug_raise_nothing(db):
    ctrl = MagicMock()
    ctrl.set_native_mode = MagicMock(return_value=(True, "ok"))
    h = CommandHandler(ctrl, db, modbus_lock=asyncio.Lock())
    await h.handle_command("operating_mode", "TOU")
    await h.handle_command("battery_command_power", "1000")
    await h.handle_command("no_such_slug", "1")
    assert await _alerts(db) == []


# ── Debounce ────────────────────────────────────────────────────────


def test_debounce_boundaries():
    d = Debounce(300, 120)
    assert d.update(True, T0) is None
    assert d.update(True, T0 + 299) is None
    assert d.update(True, T0 + 300) == "open"
    assert d.update(True, T0 + 400) is None  # once
    assert d.update(False, T0 + 401) is None
    assert d.update(True, T0 + 450) is None  # a bad blip restarts the good clock
    assert d.update(False, T0 + 460) is None
    assert d.update(False, T0 + 579) is None
    assert d.update(False, T0 + 580) == "resolve"


# ── The monitor ─────────────────────────────────────────────────────


def _gateway(gw_id="gw1", health="connected", last_good=T0, block_missing=False):
    return SimpleNamespace(
        gateway_id=gw_id,
        config=SimpleNamespace(enabled=True, name="Garage aGate", host="192.0.2.50",
                               port=502, poll_interval=10),
        status=SimpleNamespace(health=health),
        poller=SimpleNamespace(state=SimpleNamespace(
            last_good_ts=last_good, block_missing=block_missing)),
    )


@pytest.fixture
def reg():
    return workers.WorkerRegistry()


def _monitor(db, gws, reg):
    return AlertMonitor(db, SimpleNamespace(instances=gws), worker_registry=reg)


async def _run(mon, start, end, step=15, before=None):
    t = start
    while t <= end:
        if before:
            before(t)
        await mon.check(now=t)
        t += step


async def test_unreachable_opens_at_5_min_and_resolves_after_2(db, reg):
    gw = _gateway(health="unreachable")
    mon = _monitor(db, {"gw1": gw}, reg)

    def fresh(t):
        gw.poller.state.last_good_ts = t  # reads fine; only the TCP probe fails

    await _run(mon, T0, T0 + 285, before=fresh)
    assert await _alerts(db) == []
    await _run(mon, T0 + 300, T0 + 300, before=fresh)
    [a] = await _alerts(db)
    assert (a["code"], a["gateway_id"], a["resolved_ts"]) == ("GATEWAY_UNREACHABLE", "gw1", None)
    assert "Garage aGate (192.0.2.50:502) hasn't answered for 5 minutes" in a["message"]

    gw.status.health = "connected"
    await _run(mon, T0 + 315, T0 + 420, before=fresh)
    assert (await _alerts(db))[0]["resolved_ts"] is None
    await _run(mon, T0 + 435, T0 + 435, before=fresh)
    assert (await _alerts(db))[0]["resolved_ts"] == T0 + 435


async def test_live_data_missing_and_partial(db, reg):
    gw = _gateway(last_good=T0)
    mon = _monitor(db, {"gw1": gw}, reg)
    # No read since T0: bad over 30 s (3 polls), first seen at T0 + 45; open 2 min later.
    await _run(mon, T0, T0 + 150)
    assert await _alerts(db) == []
    await _run(mon, T0 + 165, T0 + 165)
    [a] = await _alerts(db)
    assert a["code"] == "LIVE_DATA_MISSING" and a["severity"] == "critical"

    # Reads come back, but without 15500-15509: live resolves, partial opens.
    gw.poller.state.block_missing = True

    def fresh(t):
        gw.poller.state.last_good_ts = t

    await _run(mon, T0 + 180, T0 + 300, before=fresh)
    live = {a["code"]: a for a in await _alerts(db)}
    assert live["LIVE_DATA_MISSING"]["resolved_ts"] == T0 + 240
    assert live["DATA_MISSING_PARTIAL"]["resolved_ts"] is None
    assert live["DATA_MISSING_PARTIAL"]["severity"] == "warning"
    assert "15500-15509" in live["DATA_MISSING_PARTIAL"]["message"]


async def test_partial_is_not_raised_when_nothing_reads(db, reg):
    gw = _gateway(last_good=None, block_missing=True)
    mon = _monitor(db, {"gw1": gw}, reg)
    await _run(mon, T0, T0 + 600)
    assert [a["code"] for a in await _alerts(db)] == ["LIVE_DATA_MISSING"]


async def test_open_condition_survives_restart_and_resolves(db, reg):
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "down", gateway_id="gw1",
                                     now=T0 - 3600)
    mon = _monitor(db, {"gw1": _gateway(last_good=None)}, reg)
    await mon.load_open()
    gw = mon._registry.instances["gw1"]

    def fresh(t):
        gw.poller.state.last_good_ts = t

    await _run(mon, T0, T0 + 120, before=fresh)
    [a] = await _alerts(db)
    assert a["resolved_ts"] == T0 + 120  # 2 min good, no second alert


async def test_removed_gateway_resolves_its_conditions(db, reg):
    gws = {"gw1": _gateway(health="unreachable", last_good=T0 + 10_000)}
    mon = _monitor(db, gws, reg)
    await _run(mon, T0, T0 + 300)
    gws.clear()
    await mon.check(now=T0 + 315)
    [a] = await _alerts(db)
    assert a["resolved_ts"] == T0 + 315


async def test_worker_stalled_opens_per_scope_and_resolves_on_beat(db, reg):
    reg.register("poller:gw1", "Modbus polling", 60, scope="gw1")
    reg.register("scheduler", "Schedules", 15)
    w_poll, w_sched = reg.workers()
    w_poll.last_beat_ts = w_sched.last_beat_ts = T0
    mon = _monitor(db, {}, reg)

    await mon.check(now=T0 + 60)  # scheduler stale after 60 s: not yet
    assert await _alerts(db) == []
    await mon.check(now=T0 + 61)
    [a] = await _alerts(db)
    assert (a["code"], a["gateway_id"]) == ("WORKER_STALLED", "")
    assert "Schedules (scheduler) has not run for over 60 s" in a["message"]

    await mon.check(now=T0 + 241)  # the poller too, in its gateway's scope
    by_scope = {a["gateway_id"]: a for a in await _alerts(db)}
    assert "Modbus polling (poller:gw1)" in by_scope["gw1"]["message"]
    last = by_scope[""]["last_ts"]
    await mon.check(now=T0 + 256)
    assert {a["gateway_id"]: a for a in await _alerts(db)}[""]["last_ts"] == last  # not rewritten

    reg.beat("scheduler", now=T0 + 260)
    await mon.check(now=T0 + 261)
    by_scope = {a["gateway_id"]: a for a in await _alerts(db)}
    assert by_scope[""]["resolved_ts"] == T0 + 261
    assert by_scope["gw1"]["resolved_ts"] is None


async def test_crashed_task_and_workers_endpoint(db, reg, monkeypatch):
    async def boom():
        raise RuntimeError("socket closed")

    task = asyncio.create_task(boom())
    await asyncio.sleep(0)
    reg.register("health_checker", "aGate reachability", 60, task=task)
    reg.register("log_flush", "Log and audit writes", 5)
    mon = _monitor(db, {}, reg)
    await mon.check()
    [a] = await _alerts(db)
    assert "aGate reachability (health_checker) stopped with an error: " \
           "RuntimeError: socket closed" in a["message"]

    monkeypatch.setattr(workers, "REGISTRY", reg)
    snap = await health_workers()
    assert snap["ok"] is False and snap["broken"] == ["health_checker"]
    assert snap["counts"] == {"CRASHED": 1, "RUNNING": 1}
    assert {w["name"]: w["stale_after_s"] for w in snap["workers"]} == {
        "health_checker": 240, "log_flush": 30}
