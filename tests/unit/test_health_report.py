"""#296 item 51 phase 1: the daily snapshot and the daily bridge-health report."""

from __future__ import annotations

import asyncio
import json
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
    await hr.set_weekly(conn, False)  # the daily tests; the weekly ones switch it on
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
    # Secrets never reach a report.
    seeded = "Seeded admin user 'admin' with a GENERATED password: Dhls6n8NoI0u — change it"
    assert hr.redact(seeded) == ("Seeded admin user 'admin' with a GENERATED password: <hidden> "
                                 "— change it")
    assert "abc123" not in hr.pattern("HA token abc123 rejected")
    assert hr.redact("api_key=XYZ failed") == "api_key=<hidden> failed"
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


async def test_snapshot_waits_for_fresh_points(db):
    """Just after a restart the gateway has no fresh reading yet: its row waits."""
    now = {"t": AT_0005}
    pts = {"ts": None}
    rep = hr.HealthReporter(db, gateways_fn=lambda: ["default"],
                            points_fn=lambda gw: _points(pts["ts"])(gw), now_fn=lambda: now["t"])
    await rep.tick()
    assert [r["gateway_id"] for r in await hr.snapshots(db)] == [""]
    pts["ts"] = AT_0005 - 3600  # stale
    await rep.tick()
    assert await hr.snapshots(db, "default") == []
    now["t"] = pts["ts"] = AT_0005 + 60
    await rep.tick()
    gw = (await hr.snapshots(db, "default"))[0]
    assert gw["day"] == "2026-11-09" and gw["soh"] == 94.8 and gw["ts"] == AT_0005 + 60


async def test_green_when_nothing_is_wrong(db):
    workers.REGISTRY.register("poller:default", "poll", 10, scope="default").last_beat_ts = AT_0700
    await db.execute("INSERT INTO gateways (id, name, host, port, unit_id, enabled, created_at) "
                     "VALUES ('spare', 'aGate', '', 502, 1, 1, 0)")  # no address yet
    r = await hr.build_daily(db, AT_0700)
    assert r["level"] == hr.GREEN and r["reasons"] == []
    assert [g["gateway"] for g in r["link"]] == ["default"]
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


async def test_resolved_but_unacknowledged_alerts_do_not_grade(db):
    # #473: 62 stalls that each cleared, never acknowledged, kept the report red.
    for k in range(3):
        await alert_store.open_condition(db, "WORKER_STALLED", "stalled",
                                         now=AT_0700 - 9000 + k * 100)
        await alert_store.resolve_condition(db, "WORKER_STALLED", now=AT_0700 - 8950 + k * 100)
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "No reply",
                                     gateway_id="default", now=AT_0700 - 7000)
    await alert_store.resolve_condition(db, "GATEWAY_UNREACHABLE", gateway_id="default",
                                        now=AT_0700 - 6000)
    await alert_store.raise_event(db, "ADMIN_ACTION", "Gateway emu2 edited",
                                  gateway_id="default", now=AT_0700 - 5000)
    assert len(await alert_store.list_alerts(db, open_only=True)) == 5  # the Alerts bar's rule
    r = await hr.build_daily(db, AT_0700)
    assert r["alerts"]["open"] == [] and r["level"] == hr.GREEN
    # Still active: graded as before.
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "No reply",
                                     gateway_id="default", now=AT_0700 - 100)
    r = await hr.build_daily(db, AT_0700)
    assert [a["code"] for a in r["alerts"]["open"]] == ["GATEWAY_UNREACHABLE"]
    assert r["level"] == hr.RED


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


async def test_no_secret_and_no_page_long_line_in_a_report(db):
    await insert_logs(db, _logs("WARNING", "Seeded admin user 'admin' with a GENERATED "
                                "password: Dhls6n8NoI0u", AT_0700 - 600, 1))
    await insert_logs(db, _logs("WARNING", "=" * 70 + " UNOFFICIAL SOFTWARE " + "x" * 3000,
                                AT_0700 - 600, 1))
    r = await hr.build_daily(db, AT_0700)
    assert "Dhls6n8NoI0u" not in json.dumps(r)
    assert max(len(p["example"]) for p in r["logs"]["patterns"]) == hr.EXAMPLE_MAX


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
        "daily": False, "daily_at": "07:00", "weekly": False, "weekly_at": "Monday 07:00"}
    assert (await client.put("/api/health/settings", json={"weekly": True})).json()["weekly"]
    wk = (await client.post("/api/health/report?kind=weekly")).json()
    assert wk["report"]["kind"] == "weekly" and wk["level"] == hr.INFO
    assert (await client.get("/api/health/report?kind=weekly")).json()["id"] == wk["id"]
    assert [r["kind"] for r in (await client.get("/api/health/reports?kind=weekly")).json()[
        "reports"]] == ["weekly"]
    assert (await client.get("/api/health/report?kind=monthly")).status_code == 422
    assert (await client.get("/api/health/snapshots")).json() == {"snapshots": []}


async def test_stop_is_clean(db):
    rep = hr.HealthReporter(db, gateways_fn=lambda: [], points_fn=lambda gw: {},
                            now_fn=lambda: AT_0659, interval_s=0.01)
    await rep.start()
    await asyncio.sleep(0.05)
    await rep.stop()


# ── weekly battery report (phase 2) ──────────────────────────

# The week covered from Tuesday 10 Nov: Monday 2 Nov to Sunday 8 Nov.
MON_0700 = datetime(2026, 11, 9, 7).timestamp()


async def _snap(db, day, soh, soc, charged, discharged, **kw):
    cols = {"gateway_id": "default", "day": day, "ts": 0, "soh": soh, "wh_rating": 13600,
            "wh_available": 6000, "soc": soc, "battery_charged_wh": charged,
            "battery_discharged_wh": discharged, "grid_import_wh": kw.get("imp", 0),
            "grid_export_wh": kw.get("exp", 0), "solar_wh": kw.get("pv", 0),
            "inverter_out_wh": None, "inverter_in_wh": None}
    await db.execute(f"INSERT INTO health_daily ({', '.join(cols)}) VALUES "
                     f"({', '.join('?' * len(cols))})", tuple(cols.values()))


def test_efficiency_needs_matching_ends():
    rows = [{"day": "2026-11-01", "soc": 50, "battery_charged_wh": 0, "battery_discharged_wh": 0},
            {"day": "2026-11-03", "soc": 80, "battery_charged_wh": 20000,
             "battery_discharged_wh": 10000},
            {"day": "2026-11-05", "soc": 52, "battery_charged_wh": 40000,
             "battery_discharged_wh": 36000},
            {"day": "2026-11-06", "soc": 90, "battery_charged_wh": 50000,
             "battery_discharged_wh": 40000}]
    e = hr.efficiency(rows)
    assert (e["pct"], e["days"], e["from"], e["to"]) == (90.0, 4, "2026-11-01", "2026-11-05")
    assert hr.efficiency(rows[1:2] + rows[3:]) is None  # 80 vs 90: too far apart


def test_soc_cycles_by_depth():
    three_deep = [10, 90] * 3 + [10]
    c = hr.soc_cycles(three_deep)
    assert c["cycles"] == 3.0 and c["bands"]["80 and over"] == 3.0 and c["mean_depth"] == 80
    shallow = [50, 60, 50, 60, 50, 50.4, 50]  # two 10-point cycles; 0.4 is noise
    c = hr.soc_cycles(shallow)
    assert c["cycles"] == 2.0 and c["bands"]["under 20"] == 2.0
    assert hr.soc_cycles([50])["cycles"] == 0.0


async def test_weekly_report(db):
    # 60 days back for the SoH trend, then the week (Sun 1 Nov .. Sun 8 Nov).
    await _snap(db, "2026-09-09", 95.6, 50, 1_000_000, 900_000)
    for i, day in enumerate(["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
                             "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]):
        await _snap(db, day, 95.0 if i < 7 else 94.8, 50 + (i % 2) * 30,
                    2_000_000 + i * 10_000, 1_800_000 + i * 9_000,
                    imp=i * 5000, exp=i * 8000, pv=i * 20_000)
    t0 = datetime(2026, 11, 2).timestamp()
    await db.executemany("INSERT INTO metrics (ts, soc, gateway_id) VALUES (?, ?, 'default')",
                         [(t0 + k * 3600, 20 if k % 12 == 0 else 90 if k % 12 == 6 else 55)
                          for k in range(0, 7 * 24)])
    await db.commit()
    r = await hr.build_weekly(db, MON_0700)
    assert r["week"] == {"from": "2026-11-02", "to": "2026-11-08"} and r["level"] == hr.INFO
    g = r["gateways"][0]
    assert g["snapshots"] == 8 and g["soh"]["now"] == 94.8 and g["soh"]["week_ago"] == 95.0
    assert g["soh"]["per_month"] == round((94.8 - 95.6) / 60 * 30, 2)
    e = g["energy"]
    assert (e["days"], e["battery_in_kwh"], e["battery_out_kwh"], e["grid_export_kwh"],
            e["solar_kwh"], e["inverter_out_kwh"]) == (7, 70.0, 63.0, 56.0, 140.0, None)
    # Ends at SoC 50 and 50 (days 0 and 6): out/in over those 6 days = 54/60.
    assert g["efficiency"]["pct"] == 90.0 and g["efficiency"]["days"] == 6
    assert g["full_cycles"] == {"week": round(70000 / 13600, 2),
                                "lifetime": round(2_070_000 / 13600, 1)}
    # 14 cycles of 70 points; the first and last are half cycles split at the week's ends.
    assert g["cycles"]["cycles"] == 14.0 and g["cycles"]["bands"]["50 to 80"] == 13.5
    assert r["headline"] == "Week of 2 Nov: SoH 94.8%, efficiency 90%, 5.15 full cycles"


async def test_weekly_with_too_few_snapshots(db):
    await _snap(db, "2026-11-08", None, None, None, None)
    await db.commit()
    r = await hr.build_weekly(db, MON_0700)
    g = r["gateways"][0]
    assert g["energy"] is None and g["efficiency"] is None and g["full_cycles"]["week"] is None
    assert r["headline"] == "Week of 2 Nov: not enough daily snapshots yet"


async def test_weekly_on_monday_once_and_catch_up(db):
    await hr.set_weekly(db, True)
    await hr.set_daily(db, False)
    ha, act = _Ha(), _Act()
    now = {"t": MON_0700 - 60}
    rep = hr.HealthReporter(db, gateways_fn=lambda: [], points_fn=lambda gw: {},
                            ha_registry=ha, actionable=act, now_fn=lambda: now["t"])
    await rep.tick()
    assert await hr.list_reports(db, "weekly") == []  # Monday 06:59: not yet
    now["t"] = MON_0700
    out = await rep.tick()
    assert out["report"]["kind"] == "weekly"
    latest = await hr.latest_report(db, "weekly")
    assert latest["period"] == "2026-11-02"
    assert ha.calls[-1][2]["notification_id"] == hr.WEEKLY_NOTIFICATION_ID
    assert ha.calls[-1][2]["title"].endswith("weekly battery report") and act.sent == []
    now["t"] = MON_0700 + 86400  # Tuesday: already done
    await rep.tick()
    assert len(await hr.list_reports(db, "weekly")) == 1
    # Down all Monday: Tuesday makes it.
    await db.execute("DELETE FROM health_reports WHERE period = '2026-11-02'")
    await db.commit()
    await rep.tick()
    assert (await hr.latest_report(db, "weekly"))["period"] == "2026-11-02"


# ── unusual-change flags (phase 3) ───────────────────────────

async def _history(db, days, last=None):
    """``days`` ordinary days to 2026-11-08 (10 kWh in, 9 out, SoC 50, SoH 95, 20 kWh
    solar, 10 warnings), then ``last`` (a dict of overrides) for 2026-11-09."""
    start = datetime(2026, 11, 9).toordinal() - days
    rows = [{} for _ in range(days)] + ([last] if last is not None else [])
    tot = {"c": 1_000_000, "d": 900_000, "pv": 0, "imp": 0, "exp": 0}
    for i, o in enumerate(rows):
        day = datetime.fromordinal(start + i).strftime("%Y-%m-%d")
        tot["c"] += o.get("c", 10_000)
        tot["d"] += o.get("d", 9_000)
        tot["pv"] += o.get("pv", 20_000)
        tot["imp"] += 5_000
        tot["exp"] += 8_000
        await _snap(db, day, o.get("soh", 95.0), 50, tot["c"], tot["d"],
                    pv=tot["pv"], imp=tot["imp"], exp=tot["exp"])
        await db.execute("INSERT INTO health_daily (gateway_id, day, ts, log_warnings, "
                         "log_errors, alerts, alarms) VALUES ('', ?, 0, ?, 0, 0, 0)",
                         (day, o.get("warn", 10)))
    await db.commit()


async def test_unusual_quiet_day_has_no_flags(db):
    await _history(db, 28, {})
    r = await hr.build_daily(db, AT_0700)
    u = r["unusual"]
    assert u["day"] == "2026-11-09" and u["flags"] == []
    assert u["checked"] == 7 + 4  # SoH, efficiency, 5 energies; the bridge's 4 counts
    assert r["level"] == hr.GREEN


async def test_unusual_flags_and_only_soh_and_efficiency_grade(db):
    await _history(db, 28, {"soh": 93.5, "d": 3_000, "pv": 2_000, "warn": 50})
    r = await hr.build_daily(db, AT_0700)
    flags = {(f["name"], f["metric"]): f for f in r["unusual"]["flags"]}
    assert set(flags) == {("aGate", "soh"), ("aGate", "efficiency"),
                          ("aGate", "battery_discharged_wh"), ("aGate", "solar_wh"),
                          ("Bridge", "log_warnings")}
    assert flags[("aGate", "soh")]["direction"] == "drop"
    assert flags[("aGate", "battery_discharged_wh")]["direction"] == "low"
    assert flags[("Bridge", "log_warnings")]["direction"] == "high"
    # 7 days to yesterday: 70 kWh in, 6 × 9 + 3 = 57 out.
    assert flags[("aGate", "efficiency")]["value"] == 81.4
    assert [f["grades"] for f in r["unusual"]["flags"] if f["metric"] in ("soh", "efficiency")] \
        == [True, True]
    assert r["level"] == hr.AMBER
    assert r["reasons"] == ["aGate: SoH down, 93.5% (usual 95%)",
                            "aGate: efficiency down, 81.4% (usual 90%)"]
    _, msg = hr.notification_text(r)
    assert "Unusual yesterday:\n- aGate: battery discharged low, 3.0 kWh (usual 9.0 kWh)" in msg
    assert "- Bridge: warnings high, 50 (usual 10)" in msg


async def test_unusual_needs_a_week_of_history(db):
    await _history(db, 5, {"soh": 80.0, "warn": 500})
    u = (await hr.build_daily(db, AT_0700))["unusual"]
    assert u == {"day": "2026-11-09", "checked": 0, "flags": []}
