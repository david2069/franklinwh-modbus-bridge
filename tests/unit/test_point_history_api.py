"""The REST layer over ``metric_samples``.

``store.point_history`` had been writing and purging this table for a while with
nothing able to read it back — the AC/DC charts were live-only, so "chart
yesterday afternoon" had no route to ask. These cover the contract that makes
the static charts and the Settings panel possible: a span resolved to something
drawable, and an empty result that says *why* it is empty.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.store import point_history


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


async def _seed(rows):
    """Write samples straight to the table the endpoint reads."""
    db = app.state.db
    await db.executemany(
        "INSERT INTO metric_samples (gateway_id, point_id, ts, value, quality) "
        "VALUES (?, ?, ?, ?, 'ok')",
        rows,
    )
    await db.commit()


# ── Config ────────────────────────────────────────────────────


async def test_history_is_off_until_asked_for(client):
    """It can add hundreds of MB to a ~20MB database. An upgrade must not start
    consuming that on an add-on running from an SD card."""
    body = (await client.get("/api/point-history/config")).json()

    assert body["config"]["enabled"] is False


async def test_config_offers_the_catalogue_a_client_can_choose_from(client):
    body = (await client.get("/api/point-history/config")).json()

    assert body["available_points"] == list(point_history.HISTORISED_POINTS)


async def test_enabling_it_sticks(client):
    await client.put("/api/point-history/config", json={"enabled": True})

    body = (await client.get("/api/point-history/config")).json()
    assert body["config"]["enabled"] is True


async def test_a_partial_patch_leaves_other_fields_alone(client):
    await client.put(
        "/api/point-history/config", json={"enabled": True, "retention_days": 30}
    )

    body = (await client.put("/api/point-history/config", json={"interval_s": 60})).json()

    assert body["config"]["retention_days"] == 30, "untouched field preserved"
    assert body["config"]["enabled"] is True
    assert body["config"]["interval_s"] == 60


async def test_an_absurd_interval_is_rejected_with_a_field_name(client):
    """422 naming the field beats a silent clamp — the user asked for 1s and
    would otherwise never learn they got 5."""
    resp = await client.put("/api/point-history/config", json={"interval_s": 1})

    assert resp.status_code == 422
    assert "interval_s" in resp.text


async def test_an_unknown_point_is_refused(client):
    resp = await client.put(
        "/api/point-history/config", json={"points": ["voltage_v", "not_a_point"]}
    )

    assert resp.status_code == 422
    assert "not_a_point" in resp.text


async def test_the_projection_moves_with_the_config(client):
    """Settings shows the cost of a choice while it is still a choice."""
    small = (
        await client.put("/api/point-history/config", json={"retention_days": 7})
    ).json()["projected_bytes"]
    large = (
        await client.put("/api/point-history/config", json={"retention_days": 90})
    ).json()["projected_bytes"]

    assert large > small


# ── Series ────────────────────────────────────────────────────


async def test_a_span_returns_the_samples_in_it(client):
    now = time.time()
    await _seed([("default", "voltage_v", now - 60 * i, 240.0 + i) for i in range(5)])

    body = (
        await client.get(
            f"/api/point-history/series?points=voltage_v&start={now - 3600}&end={now + 1}"
        )
    ).json()

    assert body["counts"]["voltage_v"] > 0


async def test_samples_outside_the_span_are_excluded(client):
    now = time.time()
    await _seed([
        ("default", "voltage_v", now - 10, 240.0),        # inside
        ("default", "voltage_v", now - 86400 * 3, 999.0),  # three days back
    ])

    body = (
        await client.get(
            f"/api/point-history/series?points=voltage_v&start={now - 3600}&end={now + 1}"
        )
    ).json()

    values = [p["value"] for p in body["series"]["voltage_v"]]
    assert 999.0 not in values


async def test_several_points_come_back_keyed_separately(client):
    now = time.time()
    await _seed([
        ("default", "voltage_v", now - 10, 240.0),
        ("default", "frequency_hz", now - 10, 50.0),
    ])

    body = (
        await client.get(
            f"/api/point-history/series?points=voltage_v,frequency_hz"
            f"&start={now - 3600}&end={now + 1}"
        )
    ).json()

    assert set(body["series"]) == {"voltage_v", "frequency_hz"}


async def test_a_requested_point_with_no_data_is_present_but_empty(client):
    """A missing key would break a chart that built its datasets from the
    request; an empty list draws an empty series, which is the truth."""
    now = time.time()

    body = (
        await client.get(
            f"/api/point-history/series?points=soh&start={now - 3600}&end={now + 1}"
        )
    ).json()

    assert body["series"]["soh"] == []


async def test_a_long_span_is_bucketed_down_to_something_drawable(client):
    """A month at 10s is ~250k samples per series and no canvas can show them."""
    now = time.time()
    body = (
        await client.get(
            f"/api/point-history/series?points=voltage_v"
            f"&start={now - 30 * 86400}&end={now}"
        )
    ).json()

    assert body["bucket_s"] > 60, "auto-bucket scaled with the span"


async def test_bucket_zero_means_raw_samples(client):
    now = time.time()
    await _seed([("default", "voltage_v", now - i, 240.0 + i) for i in range(3)])

    body = (
        await client.get(
            f"/api/point-history/series?points=voltage_v"
            f"&start={now - 60}&end={now + 1}&bucket_s=0"
        )
    ).json()

    assert body["bucket_s"] is None
    assert body["counts"]["voltage_v"] == 3, "every sample, unaveraged"


async def test_bucketing_averages_rather_than_samples(client):
    """Averaging is what keeps a bucketed chart honest — picking one value per
    bucket would hide the rest of the bucket entirely."""
    now = time.time()
    await _seed([
        ("default", "voltage_v", now - 5, 200.0),
        ("default", "voltage_v", now - 4, 300.0),
    ])

    body = (
        await client.get(
            f"/api/point-history/series?points=voltage_v"
            f"&start={now - 10}&end={now}&bucket_s=60"
        )
    ).json()

    assert body["series"]["voltage_v"][0]["value"] == pytest.approx(250.0)


async def test_a_day_resolves_to_local_midnight(client):
    """The aGate's daily counters roll at gateway-local midnight, so a UTC day
    would disagree with every other figure on the page by the offset."""
    body = (await client.get("/api/point-history/series?points=soc&day=2026-09-20")).json()

    expected = dt.datetime(2026, 9, 20).timestamp()
    assert body["start_ts"] == expected
    assert body["end_ts"] - body["start_ts"] == 86400


async def test_the_response_says_whether_recording_is_even_on(client):
    """An empty chart has two very different causes and the user should not have
    to guess which: nothing recorded for this span, or never switched on."""
    body = (await client.get("/api/point-history/series?points=soc")).json()

    assert body["recording_enabled"] is False


async def test_an_unknown_point_is_a_400_not_an_empty_chart(client):
    resp = await client.get("/api/point-history/series?points=made_up")

    assert resp.status_code == 400
    assert "made_up" in resp.text


async def test_no_points_is_refused(client):
    resp = await client.get("/api/point-history/series?points=")

    assert resp.status_code == 400


async def test_an_inverted_span_is_refused(client):
    now = time.time()
    resp = await client.get(
        f"/api/point-history/series?points=soc&start={now}&end={now - 3600}"
    )

    assert resp.status_code == 400


async def test_an_enormous_span_is_refused(client):
    resp = await client.get("/api/point-history/series?points=soc&start=0&end=99999999999")

    assert resp.status_code == 400


async def test_a_bad_day_is_refused(client):
    resp = await client.get("/api/point-history/series?points=soc&day=20th-sept")

    assert resp.status_code == 400


# ── Storage ───────────────────────────────────────────────────


async def test_storage_reports_what_is_actually_held(client):
    now = time.time()
    await _seed([("default", "soc", now - i, 50.0) for i in range(10)])

    body = (await client.get("/api/point-history/storage")).json()

    assert body["actual"]["rows"] == 10
    assert body["actual"]["bytes"] == 10 * point_history.BYTES_PER_ROW


async def test_storage_projects_a_candidate_without_saving_it(client):
    """So Settings can show the consequence while the user is still deciding."""
    before = (await client.get("/api/point-history/config")).json()["config"]

    body = (
        await client.get("/api/point-history/storage?retention_days=365&interval_s=5")
    ).json()

    after = (await client.get("/api/point-history/config")).json()["config"]
    assert body["projected_bytes"] > 0
    assert after == before, "a projection must not mutate the config"
