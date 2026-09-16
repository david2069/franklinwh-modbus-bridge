"""GET /api/energy/flow and /api/energy/totals.

The flow endpoint is the only place in the bridge that serves *reconstructed*
numbers, so these tests are mostly about the contract that keeps them honest:
a declared day, a coverage figure, and a residual the caller can check.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app


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


async def _seed(app_ref, rows):
    """Write metrics rows straight to the table the endpoint reads."""
    db = app_ref.state.db
    await db.executemany(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc, gateway_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


# ── Shape ─────────────────────────────────────────────────────


async def test_flow_defaults_to_today(client):
    resp = await client.get("/api/energy/flow")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["day"] == dt.date.today().isoformat()
    assert set(body) == {"day", "start", "end", "gateway_id", "flows", "nodes", "quality"}


async def test_flow_reports_every_arc_even_with_no_data(client):
    """A quiet day returns zeroes, not a partial dict — the Sankey should draw
    an empty diagram rather than break on a missing key."""
    body = (await client.get("/api/energy/flow?day=2020-01-01")).json()

    assert body["flows"]["solar_to_home"] == 0.0
    assert body["flows"]["grid_to_battery"] == 0.0
    assert body["nodes"]["solar"] == 0.0


async def test_an_unparseable_day_is_rejected(client):
    resp = await client.get("/api/energy/flow?day=last-tuesday")

    assert resp.status_code == 400
    assert "YYYY-MM-DD" in resp.json()["detail"]


async def test_an_inverted_span_is_rejected(client):
    resp = await client.get("/api/energy/flow?start=2000&end=1000")

    assert resp.status_code == 400


async def test_an_absurd_span_is_rejected(client):
    """Guards the query, not the caller's taste — an unbounded span would scan
    the whole archive."""
    resp = await client.get("/api/energy/flow?start=0&end=999999999")

    assert resp.status_code == 400


# ── Reconstruction over real rows ─────────────────────────────


async def test_a_sunny_hour_is_attributed_to_the_right_arcs(client):
    """3 kW of solar: 1 kW to the house, 1.2 kW into the battery, 0.8 kW out."""
    start = time.time() - 3600
    await _seed(app, [
        (start, -1200, -800, 3000, 1000, 50, "default"),
        (start + 1800, -1200, -800, 3000, 1000, 55, "default"),
        (start + 3600, -1200, -800, 3000, 1000, 60, "default"),
    ])

    body = (await client.get(
        f"/api/energy/flow?start={start - 1}&end={start + 3601}")).json()

    f = body["flows"]
    assert f["solar_to_home"] == pytest.approx(1.0, abs=0.05)
    assert f["solar_to_battery"] == pytest.approx(1.2, abs=0.05)
    assert f["solar_to_grid"] == pytest.approx(0.8, abs=0.05)
    assert f["grid_to_home"] == 0.0


async def test_nodes_and_flows_agree(client):
    start = time.time() - 3600
    await _seed(app, [
        (start, 2200, -2000, 0, 200, 80, "default"),
        (start + 3600, 2200, -2000, 0, 200, 70, "default"),
    ])

    body = (await client.get(
        f"/api/energy/flow?start={start - 1}&end={start + 3601}")).json()

    f, n = body["flows"], body["nodes"]
    assert n["grid_export"] == pytest.approx(f["solar_to_grid"] + f["battery_to_grid"])
    assert n["battery_discharge"] == pytest.approx(
        f["battery_to_home"] + f["battery_to_grid"])


async def test_quality_reports_coverage_and_residual(client):
    """The caller must be able to tell a data gap from a quiet day."""
    start = time.time() - 3600
    await _seed(app, [
        (start, 0, 0, 1000, 1000, 50, "default"),
        (start + 60, 0, 0, 1000, 1000, 50, "default"),
    ])

    q = (await client.get(
        f"/api/energy/flow?start={start - 1}&end={start + 3601}")).json()["quality"]

    assert q["samples"] == 2
    assert 0.0 <= q["coverage"] <= 1.0
    assert q["coverage"] < 0.1, "two samples cannot cover a whole hour"
    assert "residual_kwh" in q


async def test_another_gateways_data_is_not_counted(client):
    start = time.time() - 3600
    await _seed(app, [
        (start, 0, 0, 5000, 5000, 50, "other"),
        (start + 3600, 0, 0, 5000, 5000, 50, "other"),
    ])

    body = (await client.get(
        f"/api/energy/flow?start={start - 1}&end={start + 3601}&gateway=default")).json()

    assert body["flows"]["solar_to_home"] == 0.0


async def test_today_is_not_padded_out_to_midnight(client):
    """Querying "today" at noon must not count the unelapsed afternoon as an
    outage — coverage would read ~0.5 on a fully-sampled morning."""
    body = (await client.get("/api/energy/flow")).json()

    assert body["quality"]["covered_s"] <= time.time() - body["start"] + 1


# ── Totals ────────────────────────────────────────────────────


async def test_totals_expose_every_period(client):
    resp = await client.get("/api/energy/totals")

    # 503 when no gateway has reported yet is a valid answer in a bare test
    # app; the contract under test is the shape when it does.
    if resp.status_code == 503:
        pytest.skip("no cached gateway points in this fixture")

    body = resp.json()
    for period in ("lifetime_kwh", "today_kwh", "this_week_kwh",
                   "this_month_kwh", "ytd_kwh"):
        assert set(body[period]) == {
            "solar", "grid_import", "grid_export",
            "battery_charge", "battery_discharge",
        }


async def test_totals_for_an_unknown_gateway_are_not_served_as_zeroes(client):
    """The points source merges gateway-independent state (constants, billing,
    demand), so it is never empty. Treating "non-empty" as "gateway exists"
    would answer a typo'd id with a full page of nulls, which reads as a
    gateway that generated nothing rather than one that does not exist."""
    resp = await client.get("/api/energy/totals?gateway=nope")

    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]
