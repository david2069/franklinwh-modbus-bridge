"""#296 item 51 phase 1: the daily snapshot and the daily bridge-health report."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge import health_report as hr
from franklinwh_bridge import workers
from franklinwh_bridge.alert_delivery import set_ha_channel
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.health_report_api import router as health_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store.db import create_user, init_db, insert_logs

# A Tuesday, site (local) time.
DAY = datetime(2026, 11, 10)
AT_0005 = DAY.replace(minute=5).timestamp()
AT_0659 = DAY.replace(hour=6, minute=59).timestamp()
AT_0700 = DAY.replace(hour=7).timestamp()

AEMO = "AEMO NEM fetch failed: Server error '500 Internal Server Error' for url " \
       "'https://visualisations.aemo.com.au/aemo/apps/api/report/5MIN'"


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "REGISTRY", workers.WorkerRegistry())
    conn = await init_db(tmp_path / "h.db")
    await conn.execute("INSERT OR IGNORE INTO gateways (id, name, host, port, unit_id, enabled, "
                       "created_at) VALUES ('default', 'aGate', '192.0.2.10', 502, 1, 1, 0)")
    await conn.commit()
    yield conn
    await conn.close()


def _logs(level, message, start, n, step=240.0, name="franklinwh_bridge.x"):
    return [{"ts": start + i * step, "level": level, "name": name, "message": message,
             "gateway_id": ""} for i in range(n)]


class _Ha:
    def __init__(self):
        self.calls = []

    def primary(self):
        ha = self

        class _I:
            name = "Home"

            async def call_action(self, domain, service, data):
                ha.calls.append((domain, service, data))
                return {"ok": True}
        return _I()


class _Act:
    def __init__(self):
        self.sent = []

    async def notify_health(self, title, message):
        self.sent.append((title, message))
        return SimpleNamespace(summary="1/1")


def _points(sample_ts):
    return lambda gw: {"_sample_ts": sample_ts, "soh": 94.8, "wh_rating": 13600,
                       "wh_available": 12900, "soc": 55, "dc_energy_charged_wh": 4_000_000,
                       "dc_energy_discharged_wh": 3_600_000, "grid_import_wh": 9_000_000,
                       "grid_export_wh": 2_000_000, "pv_energy_total_wh": 11_000_000,
                       "701.TotWhInj": 3_500_000}


def test_pattern_groups_repeats_and_hides_addresses():
    a = hr.pattern(AEMO)
    assert a == hr.pattern(AEMO.replace("500", "502"))
    assert "<addr>" in a and "#" in a
    assert hr.pattern("Gateway default: poll failed after 3.5 s") == \
        hr.pattern("Gateway default: poll failed after 10.0 s")
    assert hr.pattern("connect to 192.0.2.10:502 refused") == "connect to <addr> refused"
    # Names with digits in them stay (M701, v12).
    assert "M701" in hr.pattern("M701 alarm 7 set")


async def test_snapshot_written_once_after_midnight(db):
    await insert_logs(db, _logs("WARNING", "w", AT_0005 - 3600, 3)
                      + _logs("ERROR", "e", AT_0005 - 3600, 1))
    rep = hr.HealthReporter(db, gateways_fn=lambda: ["default"],
                            points_fn=_points(AT_0005 - 10), now_fn=lambda: AT_0005)
    assert await rep.tick() is None  # no report before 07:00
    rows = {r["gateway_id"]: r for r in await hr.snapshots(db)}
    assert set(rows) == {"", "default"}
    gw, bridge = rows["default"], rows[""]
    assert gw["day"] == "2026-11-09" and gw["soh"] == 94.8 and gw["battery_charged_wh"] == 4e6
    assert gw["inverter_out_wh"] == 3.5e6 and gw["inverter_in_wh"] is None
    assert bridge["log_warnings"] == 3 and bridge["log_errors"] == 1 and bridge["soh"] is None
    await rep.tick()
    assert len(await hr.snapshots(db)) == 2


async def test_snapshot_keeps_no_stale_counters(db):
    rep = hr.HealthReporter(db, gateways_fn=lambda: ["default"],
                            points_fn=_points(AT_0005 - 3600), now_fn=lambda: AT_0005)
    await rep.tick()
    gw = (await hr.snapshots(db, "default"))[0]
    assert gw["soh"] is None and gw["grid_import_wh"] is None and gw["ts"] == AT_0005


async def test_green_when_nothing_is_wrong(db):
    workers.REGISTRY.register("poller:default", "poll", 10, scope="default").last_beat_ts = AT_0700
    r = await hr.build_daily(db, AT_0700)
    assert r["level"] == hr.GREEN and r["reasons"] == []
    assert r["headline"].startswith("Green: all 1 workers running")


async def test_red_for_a_stalled_worker_and_a_critical_alert(db):
    workers.REGISTRY.register("alert_monitor", "alerts", 5).last_beat_ts = AT_0700 - 600
    workers.REGISTRY.check(AT_0700)
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "No reply",
                                     gateway_id="default", now=AT_0700 - 100)
    r = await hr.build_daily(db, AT_0700)
    assert r["level"] == hr.RED
    assert "worker alert_monitor stopped beating" in r["reasons"]
    assert any(x.startswith("alert open: aGate unreachable (default)") for x in r["reasons"])


async def test_log_patterns_new_growing_all_day(db):
    day0 = AT_0700 - 86400
    rows = (_logs("WARNING", AEMO, day0 + 60, 360)            # every 4 min, all day: new
            + _logs("WARNING", "Slow poll 2.0 s", day0 - 3 * 86400, 7)   # 1 a day before
            + _logs("WARNING", "Slow poll 9.0 s", day0 + 60, 12, step=60)  # 12 today: growing
            + _logs("WARNING", "Once 1", day0 + 60, 1))          # new but rare
    await insert_logs(db, rows)
    r = await hr.build_daily(db, AT_0700)
    pats = {p["pattern"]: p for p in r["logs"]["patterns"]}
    aemo = pats[hr.pattern(AEMO)]
    assert aemo["count"] == 360 and aemo["flags"] == ["new", "all day"] and aemo["per_hour"] == 15
    slow = pats[hr.pattern("Slow poll 1 s")]
    assert slow["count"] == 12 and slow["prior_7d"] == 7 and slow["flags"] == ["growing"]
    assert pats["Once #"]["flags"] == ["new"]
    assert r["level"] == hr.AMBER and r["logs"]["warnings"] == 373
    assert r["headline"].startswith("Amber: 360 × warning (new, all day): AEMO NEM fetch failed")
    assert not any("Once" in x for x in r["reasons"])  # under PATTERN_MIN


async def test_any_error_is_amber(db):
    await insert_logs(db, _logs("ERROR", "Gateway bring-up failed: boom", AT_0700 - 7200, 1))
    r = await hr.build_daily(db, AT_0700)
    assert r["level"] == hr.AMBER and r["logs"]["errors"] == 1


async def test_link_failures_amber(db):
    hour = AT_0700 - 3 * 3600
    await db.execute("INSERT INTO link_stats (gateway_id, hour_start, kind, path, count, ok, "
                     "timeout_no_reply, timeout_closed, error) VALUES "
                     "('default', ?, 'model_read', 'poller', 360, 330, 20, 5, 5)", (hour,))
    await db.execute("INSERT INTO link_stats (gateway_id, hour_start, kind, path, reconnects, "
                     "polls, max_consec_fail) VALUES ('default', ?, 'link', '', 3, 360, 4)",
                     (hour,))
    await db.commit()
    r = await hr.build_daily(db, AT_0700)
    gw = r["link"][0]
    assert (gw["requests"], gw["failed"], gw["reconnects"], gw["max_consec_fail"]) == (
        360, 30, 3, 4)
    assert r["level"] == hr.AMBER and "aGate: 8% of Modbus requests failed" in r["reasons"]


async def test_report_at_seven_once_and_delivery(db):
    ha, act = _Ha(), _Act()
    now = {"t": AT_0659}
    rep = hr.HealthReporter(db, gateways_fn=lambda: [], points_fn=lambda gw: {},
                            ha_registry=ha, actionable=act, now_fn=lambda: now["t"])
    assert await rep.tick() is None
    now["t"] = AT_0700
    out = await rep.tick()
    assert out["level"] == hr.GREEN
    assert [c[2]["notification_id"] for c in ha.calls] == [hr.NOTIFICATION_ID]
    assert act.sent == []  # green: no phones
    assert await rep.tick() is None  # once a day
    latest = await hr.latest_report(db)
    assert latest["period"] == "2026-11-10" and latest["report"]["kind"] == "daily"

    # Next day, amber: HA and phones.
    await insert_logs(db, _logs("ERROR", "boom", AT_0700 + 3600, 1))
    now["t"] = AT_0700 + 86400
    out = await rep.tick()
    assert out["level"] == hr.AMBER and len(ha.calls) == 2 and len(act.sent) == 1
    assert act.sent[0][0].endswith("health amber")


async def test_switches(db):
    ha, act = _Ha(), _Act()
    rep = hr.HealthReporter(db, gateways_fn=lambda: [], points_fn=lambda gw: {},
                            ha_registry=ha, actionable=act, now_fn=lambda: AT_0700)
    await hr.set_daily(db, False)
    assert await rep.tick() is None and await hr.latest_report(db) is None
    await hr.set_daily(db, True)
    await set_ha_channel(db, False)
    await insert_logs(db, _logs("ERROR", "boom", AT_0700 - 60, 1))
    out = await rep.tick()
    assert out["level"] == hr.AMBER and ha.calls == [] and act.sent == []


@pytest.fixture
async def client(db, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    await create_user(db, "root", hash_password("pw"), role="admin")
    await create_user(db, "vee", hash_password("pw"), role="viewer")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = db
    app.state.config = SimpleNamespace(environment="dev")
    app.state.health_reporter = hr.HealthReporter(db, gateways_fn=lambda: [],
                                                  points_fn=lambda gw: {})
    app.include_router(auth_router)
    app.include_router(health_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_api(client):
    assert (await client.get("/api/health/report")).status_code == 401
    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.get("/api/health/report")).json() == {"report": None}
    assert (await client.post("/api/health/report")).status_code == 403
    assert (await client.put("/api/health/settings", json={"daily": False})).status_code == 403
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    run = (await client.post("/api/health/report")).json()
    assert run["level"] in (hr.GREEN, hr.AMBER, hr.RED) and run["report"]["kind"] == "daily"
    latest = (await client.get("/api/health/report")).json()
    assert latest["id"] == run["id"] and latest["headline"] == run["headline"]
    listed = (await client.get("/api/health/reports")).json()["reports"]
    assert [r["id"] for r in listed] == [run["id"]] and "report" not in listed[0]
    assert (await client.get(f"/api/health/reports/{run['id']}")).status_code == 200
    assert (await client.get("/api/health/reports/999")).status_code == 404
    assert (await client.put("/api/health/settings", json={"daily": False})).json() == {
        "daily": False, "daily_at": "07:00"}
    assert (await client.get("/api/health/snapshots")).json() == {"snapshots": []}


async def test_stop_is_clean(db):
    rep = hr.HealthReporter(db, gateways_fn=lambda: [], points_fn=lambda gw: {},
                            now_fn=lambda: AT_0659, interval_s=0.01)
    await rep.start()
    await asyncio.sleep(0.05)
    await rep.stop()
