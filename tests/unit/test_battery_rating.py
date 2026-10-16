"""Force commands are sized only against an evidence-backed rating (#133, #134).

#133: a site with several aPowers reported Model 702's 5000 W, so a 5 kW request
went out as 100 % and the aGate applied 100 % of 20 kW. #134: an unread rating
was silently assumed to be 5000 W.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.gateway import battery_rating as br
from franklinwh_bridge.gateway.battery_rating import BatteryRating
from franklinwh_bridge.publish import command_handler as ch
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import create_gateway, delete_gateway, init_db
from tests.conftest import calibrated_rating


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "rating.db")
    yield conn
    await conn.close()


@pytest.fixture
def ctrl():
    c = MagicMock()
    c.read_control_status.return_value = {}
    c.send_command.return_value = (True, "OK")
    c.reset_control_state.return_value = True
    c.get_model.return_value = None
    return c


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(br.time, "time", c.time)
    return c


# ── decision ─────────────────────────────────────────────────────────────


def test_unknown_rating_is_not_trusted():
    d = BatteryRating("g").decision("discharge")
    assert not d.trusted and d.source == "unknown" and "Calibrate" in d.reason


async def test_m702_alone_is_not_trusted():
    r = BatteryRating("g")
    await r.record_m702(5000, 5000)
    assert r.decision("discharge").source == "m702_unconfirmed"
    assert not r.decision("discharge").trusted


async def test_m702_confirmed_by_an_observed_peak():
    r = BatteryRating("g")
    await r.record_m702(5000, 5000)
    await r.observe({"battery_power_w": 4900})  # the owner's 18:00 discharge
    d = r.decision("discharge")
    assert d.trusted and d.rating_w == 5000 and d.source == "m702_confirmed"
    assert not r.decision("charge").trusted  # never seen charging near 5 kW


async def test_peak_far_above_m702_contradicts_it():
    """#133's site: rating says 5000 W, the battery has run at 20 kW."""
    r = BatteryRating("g")
    await r.record_m702(5000, 5000)
    await r.observe({"battery_power_w": 20000})
    d = r.decision("discharge")
    assert not d.trusted and d.source == "contradicted" and "20000" in d.reason


async def test_peak_above_a_trusted_rating_revokes_trust():
    r = BatteryRating("g")
    await r.record_m702(5000, 5000)
    await r.observe({"battery_power_w": 4900})
    assert r.decision("discharge").trusted
    await r.observe({"battery_power_w": 9000})
    assert not r.decision("discharge").trusted


async def test_absurd_and_missing_readings_are_ignored():
    r = BatteryRating("g")
    await r.observe({"battery_power_w": 196605})
    await r.observe({"battery_power_w": None})
    await r.observe({})
    assert r.dirs["discharge"].peak_w is None


def test_mock_gateway_is_always_trusted():
    assert BatteryRating("demo", mock=True).decision("charge").trusted


# ── calibration ──────────────────────────────────────────────────────────


async def test_calibration_records_the_peak_of_the_window(clock):
    r = BatteryRating("g")
    r.start_calibration("charge", soc=50)
    for w in (-3000, -9800, -10100, -9900):  # TOU force charge from grid
        await r.observe({"battery_power_w": w})
    clock.t += 120
    d = await r.finish_calibration()
    assert d.trusted and d.rating_w == 10100 and d.source == "calibrated"


async def test_calibration_too_short_or_too_weak_is_refused(clock):
    r = BatteryRating("g")
    r.start_calibration("discharge", soc=50)
    await r.observe({"battery_power_w": 9000})
    clock.t += 10
    with pytest.raises(ValueError, match="at least"):
        await r.finish_calibration()
    r.start_calibration("discharge", soc=50)
    await r.observe({"battery_power_w": 200})
    clock.t += 120
    with pytest.raises(ValueError, match="only reached"):
        await r.finish_calibration()


async def test_calibration_after_a_contradiction_restores_trust(clock):
    r = BatteryRating("g")
    await r.contradict("discharge", "overshoot")
    clock.t += 1
    r.start_calibration("discharge", soc=50)
    await r.observe({"battery_power_w": 19500})
    clock.t += 90
    assert (await r.finish_calibration()).rating_w == 19500


# ── persistence ──────────────────────────────────────────────────────────


async def test_evidence_survives_a_restart(db, clock):
    r = BatteryRating("g")
    await r.load(db)
    await r.record_m702(5000, 5000)
    await r.observe({"battery_power_w": 4950})
    again = BatteryRating("g")
    await again.load(db)
    assert again.decision("discharge").trusted


async def test_first_load_seeds_peaks_from_recorded_history(db):
    await db.execute("INSERT INTO metrics (ts, battery_w, gateway_id) VALUES (1, 4900, 'g')")
    await db.execute("INSERT INTO metrics (ts, battery_w, gateway_id) VALUES (2, -4800, 'g')")
    await db.execute("INSERT INTO metrics (ts, battery_w, gateway_id) VALUES (3, 196605, 'g')")
    await db.commit()
    r = BatteryRating("g")
    await r.load(db)
    assert r.dirs["discharge"].peak_w == 4900 and r.dirs["charge"].peak_w == 4800


async def test_deleting_a_gateway_deletes_its_rating(db):
    await create_gateway(db, "g2", name="Second", host="192.0.2.20")
    r = BatteryRating("g2")
    await r.load(db)
    await r.record_m702(5000, 5000)
    await delete_gateway(db, "g2")
    async with db.execute("SELECT COUNT(*) FROM battery_ratings WHERE gateway_id='g2'") as cur:
        assert (await cur.fetchone())[0] == 0


# ── command handler ──────────────────────────────────────────────────────


async def test_force_command_refused_without_a_trusted_rating(ctrl, db):
    h = CommandHandler(ctrl, db)  # rating unknown
    res = await h.handle_command("battery_command", "Force Discharge")
    assert res["ok"] is False and "Refused Force Discharge" in res["result"]
    ctrl.send_command.assert_not_called()


async def test_force_command_on_133s_site_is_refused(ctrl, db):
    rating = BatteryRating("default")
    await rating.record_m702(5000, 5000)
    await rating.observe({"battery_power_w": 20000})
    h = CommandHandler(ctrl, db, rating=rating)
    res = await h.handle_command("battery_command", "Force Discharge")
    assert res["ok"] is False and "20000" in res["result"]
    ctrl.send_command.assert_not_called()


async def test_trusted_rating_is_what_the_library_sizes_against(ctrl, db):
    h = CommandHandler(ctrl, db, rating=calibrated_rating(charge_w=8000, discharge_w=10000))
    await h.handle_command("battery_command_power", "5000")
    await h.handle_command("battery_command", "Force Discharge")
    assert ctrl.RATED_MAX_DISCHARGE_W == 10000
    assert ctrl.send_command.call_args[0][0].power_watts == -5000


async def test_power_percent_is_refused_outside_legacy_schedules(ctrl, db):
    h = CommandHandler(ctrl, db, rating=calibrated_rating())
    for who in ("manual", "ha", "rest"):
        res = await h.handle_command("battery_command_power_pct", "50", requester=who)
        assert res["ok"] is False and "watts" in res["result"]
    res = await h.handle_command("battery_command_power_pct", "50", requester="schedule:e1")
    assert res["ok"] is True


async def test_legacy_schedule_percent_is_sized_against_the_trusted_rating(ctrl, db):
    h = CommandHandler(ctrl, db, rating=calibrated_rating(discharge_w=10000))
    await h.handle_command("battery_command_power_pct", "100", requester="schedule:e1")
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    assert ctrl.send_command.call_args[0][0].power_watts == -10000


async def test_overshoot_guard_releases_and_revokes_trust(ctrl, db, monkeypatch):
    points = {"battery_power_w": 0}
    h = CommandHandler(ctrl, db, rating=calibrated_rating(discharge_w=5000),
                       points_getter=lambda: points)
    await h.handle_command("battery_command_power", "5000")
    await h.handle_command("battery_command", "Force Discharge")
    assert h.state.active
    t0 = h.state.started_at
    monkeypatch.setattr(ch.time, "time", lambda: t0 + ch.OVERSHOOT_CHECK_AFTER_S + 1)
    points["battery_power_w"] = 20000  # the aGate applied 100 % of 20 kW
    assert await h._overshoot_guard() is True
    assert not h.state.active
    assert "Released" in h.state.last_result and h.state.last_success is False
    assert not h.rating.decision("discharge").trusted
    ctrl.send_command.reset_mock()
    res = await h.handle_command("battery_command", "Force Discharge")
    assert res["ok"] is False
    ctrl.send_command.assert_not_called()


async def test_overshoot_guard_leaves_a_normal_dispatch_alone(ctrl, db, monkeypatch):
    points = {"battery_power_w": 0}
    h = CommandHandler(ctrl, db, rating=calibrated_rating(discharge_w=5000),
                       points_getter=lambda: points)
    await h.handle_command("battery_command_power", "5000")
    await h.handle_command("battery_command", "Force Discharge")
    t0 = h.state.started_at
    monkeypatch.setattr(ch.time, "time", lambda: t0 + ch.OVERSHOOT_CHECK_AFTER_S + 1)
    points["battery_power_w"] = 4900
    assert await h._overshoot_guard() is False and h.state.active


async def test_samples_feed_the_rating(ctrl, db):
    rating = BatteryRating("default")
    await rating.record_m702(5000, 5000)
    h = CommandHandler(ctrl, db, rating=rating)
    await h.observe_sample(SimpleNamespace(points={"battery_power_w": 4900}, ts=1.0))
    assert h.rating.decision("discharge").trusted


# ── REST ─────────────────────────────────────────────────────────────────


async def test_rating_and_calibration_routes(ctrl, db, clock):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from franklinwh_bridge.api import gateways_api

    points = {"battery_power_w": 0, "soc": 52}
    h = CommandHandler(ctrl, db, points_getter=lambda: points)
    await h.rating.record_m702(5000, 5000)
    inst = SimpleNamespace(command_handler=h)
    app = FastAPI()
    app.state.db = db
    app.state.registry = SimpleNamespace(get=lambda gid: inst if gid == "g1" else None)
    app.include_router(gateways_api.router)
    app.dependency_overrides[gateways_api._REQUIRE_CONTROL] = lambda: {"username": "owner"}
    base = "/api/gateways/g1/battery-rating"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.get(base)).json()
        assert r["discharge"]["source"] == "m702_unconfirmed" and r["soc"] == 52
        assert (await c.get("/api/gateways/g1/battery/limits")).json()["max_discharge_w"] is None
        assert (await c.get("/api/gateways/nope/battery-rating")).status_code == 404

        r = await c.post(base + "/calibration", json={"action": "start", "direction": "discharge"})
        assert r.status_code == 200 and r.json()["calibration"]["direction"] == "discharge"
        await h.observe_sample(SimpleNamespace(points={"battery_power_w": 19800}, ts=None))
        r = await c.post(base + "/calibration", json={"action": "finish"})
        assert r.status_code == 409 and "at least" in r.json()["detail"]
        clock.t += 90
        r = (await c.post(base + "/calibration", json={"action": "finish"})).json()
        assert r["discharge"]["trusted"] and r["discharge"]["rating_w"] == 19800
        lim = (await c.get("/api/gateways/g1/battery/limits")).json()
        assert lim["max_discharge_w"] == 19800 and lim["max_charge_w"] is None

        r = await c.post(base + "/calibration", json={"action": "start"})
        assert r.status_code == 422



# ── 0.2.9 hotfix ─────────────────────────────────────────────────────────


async def test_refused_command_replaces_an_earlier_ok_result(ctrl, db):
    """A refusal must not leave the previous dispatch's ok for the scheduler to read
    (0.2.8 regression: a refused schedule was audited "ok — dispatched")."""
    h = CommandHandler(ctrl, db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")
    assert h.last_dispatch.ok is True
    await h.handle_command("battery_command", "Release")
    h.rating = BatteryRating("default")  # rating no longer trusted
    res = await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    assert res["ok"] is False
    assert h.last_dispatch.ok is False and "Refused Force Discharge" in h.last_dispatch.reason


async def test_unknown_action_replaces_an_earlier_ok_result(ctrl, db):
    h = CommandHandler(ctrl, db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge")
    await h.handle_command("battery_command", "Sideways")
    assert h.last_dispatch.ok is False and "Unknown" in h.last_dispatch.reason


async def test_scheduled_empty_power_is_full_trusted_rating_not_a_manual_leftover(ctrl, db):
    from franklinwh_bridge.gateway.scheduler import action_to_commands

    h = CommandHandler(ctrl, db, rating=calibrated_rating(discharge_w=5000))
    await h.handle_command("battery_command_power", "800")       # manual leftover
    await h.handle_command("battery_command_duration", "300")    # manual leftover
    for slug, value in action_to_commands("force_discharge", {}):
        await h.handle_command(slug, value, requester="schedule:e1")
    assert ctrl.send_command.call_args[0][0].power_watts == -5000
    assert h.state.watchdog_s == 0
