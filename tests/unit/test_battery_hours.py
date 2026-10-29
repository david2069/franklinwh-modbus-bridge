"""Per-hour battery charge/discharge (#116) — the Solar Forecast card's band.

Unlike the Sankey arcs this integrates one measured scalar, ``battery_w``
(positive = discharge), so the tests pin the sign split, the edge splitting,
the gap cap and the "no data is not idle" rule.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway.energy_flow import BATTERY_IDLE_KWH, battery_by_hour
from franklinwh_bridge.main import app

H = 3600.0


def b(ts, watts):
    return {"ts": ts, "battery_w": watts}


def edges(n, start=0.0):
    return [start + i * H for i in range(n + 1)]


# ── Pure aggregation ──────────────────────────────────────────


def test_charging_hour_is_charge_and_sign_is_respected():
    rows = [b(t, -2000) for t in range(0, 3601, 60)]
    [h] = battery_by_hour(rows, edges(1))
    assert h["state"] == "charge"
    assert h["charge_kwh"] == pytest.approx(2.0, abs=0.01)
    assert h["discharge_kwh"] == 0.0


def test_mixed_hour_reports_both_directions_not_the_net():
    """40 min charging at 1.5 kW, 20 min discharging at 1.5 kW: a 1 h mean would
    read +0.5 kW net; the band must see 1.0 in and 0.5 out."""
    rows = [b(t, -1500) for t in range(0, 2400, 60)]
    rows += [b(t, 1500) for t in range(2400, 3601, 60)]
    [h] = battery_by_hour(rows, edges(1))
    assert h["charge_kwh"] == pytest.approx(1.0, abs=0.03)
    assert h["discharge_kwh"] == pytest.approx(0.5, abs=0.03)
    assert h["state"] == "charge"


def test_a_hold_crossing_an_edge_is_split_between_hours():
    rows = [b(t, 1200) for t in range(1800, 5401, 600)]
    first, second = battery_by_hour(rows, edges(2))
    assert first["discharge_kwh"] == pytest.approx(0.6, abs=0.01)
    assert second["discharge_kwh"] == pytest.approx(0.6, abs=0.01)
    assert first["state"] == second["state"] == "discharge"


def test_trickle_is_idle_and_an_unsampled_hour_is_none():
    rows = [b(t, 20) for t in range(0, 3601, 60)]
    idle, empty = battery_by_hour(rows, edges(2))
    assert idle["state"] == "idle"
    assert idle["discharge_kwh"] < BATTERY_IDLE_KWH
    assert empty["state"] is None, "no samples must not read as idle"
    assert empty["covered_s"] == 0.0


def test_an_outage_is_not_bridged():
    """Samples either side of a 5 h gap must not carry power across it."""
    rows = [b(t, -3000) for t in range(0, 601, 60)]
    rows += [b(t, -3000) for t in range(int(6 * H), int(6 * H) + 601, 60)]
    hours = battery_by_hour(rows, edges(7))
    assert [h["state"] for h in hours[1:6]] == [None] * 5
    # 10 min of samples plus the last one held for the cap (3 x the 60 s median
    # step): 780 s at 3 kW = 0.65 kWh — not 6 h of invented charge.
    assert hours[0]["charge_kwh"] == pytest.approx(0.65, abs=0.01)


def test_missing_reading_counts_as_covered_but_moves_nothing():
    rows = [b(t, None) for t in range(0, 3601, 60)]
    [h] = battery_by_hour(rows, edges(1))
    assert h["covered_s"] > 0
    assert h["charge_kwh"] == h["discharge_kwh"] == 0.0
    assert h["state"] == "idle"


def test_no_samples_returns_every_hour_empty():
    hours = battery_by_hour([], edges(24))
    assert len(hours) == 24
    assert all(h["state"] is None for h in hours)


# ── Endpoint ──────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_endpoint_shape_and_today_default(client):
    resp = await client.get("/api/energy/battery-hours?gateway=default")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["day"] == dt.date.today().isoformat()
    assert "estimate" in body["basis"]
    # 23-25 wall-clock hours, contiguous, covering the local day.
    hours = body["hours"]
    assert 23 <= len(hours) <= 25
    assert hours[0]["start"] == body["start"] and hours[-1]["end"] == body["end"]
    assert all(a["end"] == b_["start"] for a, b_ in zip(hours, hours[1:], strict=False))


async def test_endpoint_rejects_a_bad_day(client):
    resp = await client.get("/api/energy/battery-hours?day=yesterday-ish&gateway=default")
    assert resp.status_code == 400


async def test_endpoint_integrates_seeded_rows_for_the_gateway_only(client):
    now = time.time()
    day_start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
    if now - day_start < 2 * H:
        pytest.skip("too close to local midnight for a full seeded hour")
    t0 = (int((now - day_start) // H) - 1) * H + day_start   # last full hour
    rows = [(t0 + i * 60, -1800, 0, 0, 0, 50, "default") for i in range(61)]
    rows += [(t0 + i * 60, 5000, 0, 0, 0, 50, "other") for i in range(61)]
    db = app.state.db
    await db.executemany(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc, gateway_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()

    hours = (await client.get("/api/energy/battery-hours?gateway=default")).json()["hours"]
    hit = next(h for h in hours if h["start"] == t0)
    assert hit["state"] == "charge"
    assert hit["discharge_kwh"] == 0.0
    assert hit["charge_kwh"] == pytest.approx(1.8, abs=0.1)
