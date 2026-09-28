"""Tariff plan history over REST — who billed you, and when.

The store side (migration 45/46) could already express "Amber until 09 Sep, AGL
from then", but nothing could reach it: the only way to record the owner's own
switch was a hand-written script against the live database. These are the routes
that make it an ordinary edit, plus the annotation that stops a billing period
which straddles a switch from being reported under one retailer's name as though
that were unambiguous.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app


def ts(y: int, m: int, d: int) -> float:
    return dt.datetime(y, m, d).timestamp()


AUG1 = ts(2026, 8, 1)
SWITCH = ts(2026, 9, 9)      # Amber -> AGL, the real one
SEP_START = ts(2026, 9, 1)
OCT_START = ts(2026, 10, 1)


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
        r = await ac.patch("/api/modules/energy_costs", json={"enabled": True})
        assert r.status_code == 200, r.text
        yield ac


async def _backdate_opening_plan(client) -> str:
    """Put the seeded service's opening plan back in August.

    create_service() opens a plan at "now", which is right for a service added
    today — but the case under test predates the switch.
    """
    plans = (await client.get("/api/tariff/plans")).json()
    plan_id = plans["plans"][0]["id"]
    resp = await client.patch(f"/api/tariff/plans/{plan_id}", json={"valid_from": AUG1})
    assert resp.status_code == 200, resp.text
    return plans["service"]


async def _seed_period(start: float, end: float, net: float = 100.0):
    db = app.state.db
    await db.execute(
        "INSERT INTO billing_periods (gateway_id, period_start, period_end, "
        "net_total, created_at) VALUES ('default', ?, ?, ?, 0)",
        (start, end, net),
    )
    await db.commit()


# ── Reading ───────────────────────────────────────────────────


async def test_a_service_always_has_at_least_one_plan(client):
    """The invariant plan_at() relies on — without it every historical instant
    resolves to "no plan"."""
    body = (await client.get("/api/tariff/plans")).json()

    assert len(body["plans"]) >= 1


async def test_an_unknown_service_is_a_404(client):
    resp = await client.get("/api/tariff/plans?service=nope")

    assert resp.status_code == 404


# ── Recording a switch ────────────────────────────────────────


async def test_a_switch_can_be_backdated(client):
    """The normal case: you record it after it happened."""
    await _backdate_opening_plan(client)

    resp = await client.post(
        "/api/tariff/plans",
        json={"valid_from": SWITCH, "retailer": "AGL Energy", "network": "Ausgrid"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"]["retailer"] == "AGL Energy"


async def test_the_previous_plan_is_closed_at_the_switch(client):
    await _backdate_opening_plan(client)
    await client.post(
        "/api/tariff/plans", json={"valid_from": SWITCH, "retailer": "AGL Energy"}
    )

    plans = (await client.get("/api/tariff/plans")).json()["plans"]

    assert len(plans) == 2
    assert plans[0]["valid_to"] == SWITCH, "abuts its successor exactly"
    assert plans[1]["valid_to"] is None


async def test_a_plan_can_be_inserted_between_two_existing_ones(client):
    """Correcting history you did not capture at the time: Origin ran 02-09 Sep
    between the opening plan and AGL. It must be bounded by AGL's start rather
    than left open, or two open-ended plans overlap and resolution picks
    arbitrarily between them."""
    await _backdate_opening_plan(client)
    await client.post(
        "/api/tariff/plans", json={"valid_from": SWITCH, "retailer": "AGL Energy"}
    )

    resp = await client.post(
        "/api/tariff/plans", json={"valid_from": ts(2026, 9, 2), "retailer": "Origin"}
    )

    assert resp.status_code == 200, resp.text
    plans = (await client.get("/api/tariff/plans")).json()["plans"]
    assert [p["valid_from"] for p in plans] == [AUG1, ts(2026, 9, 2), SWITCH]
    origin = plans[1]
    assert origin["retailer"] == "Origin"
    assert origin["valid_to"] == SWITCH, "bounded by its successor, not left open"
    assert plans[0]["valid_to"] == ts(2026, 9, 2), "predecessor closed at the insert"


async def test_an_overlapping_start_is_a_400_not_a_written_row(client):
    """Two plans starting at the same instant is a range that can never have
    been true; the resolver would pick between them arbitrarily."""
    await _backdate_opening_plan(client)

    resp = await client.post(
        "/api/tariff/plans", json={"valid_from": AUG1, "retailer": "Origin"}
    )

    assert resp.status_code == 400
    assert "overlap" in resp.text.lower()


async def test_an_epoch_zero_start_is_refused(client):
    """0 is the placeholder migration 45 used for "unknown" — accepting it here
    would recreate the problem migration 46 exists to clean up."""
    resp = await client.post(
        "/api/tariff/plans", json={"valid_from": 0, "retailer": "Origin"}
    )

    assert resp.status_code == 422


async def test_pricing_carries_forward_without_being_restated(client):
    """A retailer switch on the same network should not require restating rates.

    The inherited value comes back DECODED from get_service(), so it has to be
    re-encoded before it reaches the driver — otherwise this is the request that
    fails with "type 'dict' is not supported", which is exactly how it failed on
    the live bridge.
    """
    svc = await _backdate_opening_plan(client)
    setup = await client.patch(f"/api/services/{svc}", json={"pricing": {"buy": 0.42}})
    assert setup.status_code == 200, f"test setup failed: {setup.text}"

    resp = await client.post(
        "/api/tariff/plans", json={"valid_from": SWITCH, "retailer": "AGL Energy"}
    )

    assert resp.status_code == 200, resp.text
    pricing = resp.json()["plan"]["pricing"]
    assert json.loads(pricing) == {"buy": 0.42}, "rates carried into the new plan"


async def test_the_settings_switch_plan_button_records_plan_history(client):
    """``start_new_plan`` is what the shipped Settings button calls. It used to
    move the service row forward without writing a service_plans row, so the
    dated history was complete only for switches made through the plans API and
    silently stale for every one made through the UI."""
    svc = await _backdate_opening_plan(client)

    resp = await client.post(
        f"/api/services/{svc}/switch-plan",
        json={"retailer": "AGL Energy", "network": "Ausgrid"},
    )
    assert resp.status_code == 200, resp.text

    plans = (await client.get("/api/tariff/plans")).json()["plans"]
    assert len(plans) == 2, "the switch is in the history, not just on the service"
    assert plans[-1]["retailer"] == "AGL Energy"
    assert plans[0]["valid_to"] == plans[1]["valid_from"], "no gap at the boundary"


# ── Correcting a start date ───────────────────────────────────


async def test_a_start_date_can_be_corrected(client):
    plans = (await client.get("/api/tariff/plans")).json()["plans"]

    resp = await client.patch(
        f"/api/tariff/plans/{plans[0]['id']}", json={"valid_from": AUG1}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"]["valid_from"] == AUG1


async def test_correcting_an_unknown_plan_is_a_404(client):
    resp = await client.patch("/api/tariff/plans/plan_nope", json={"valid_from": AUG1})

    assert resp.status_code == 404


# ── The point of all this: periods that span a switch ─────────


async def test_a_period_reports_the_plan_in_force(client):
    await _backdate_opening_plan(client)
    await _seed_period(OCT_START, ts(2026, 11, 1))
    await client.post(
        "/api/tariff/plans", json={"valid_from": SWITCH, "retailer": "AGL Energy"}
    )

    periods = (await client.get("/api/tariff/history")).json()["periods"]

    october = next(p for p in periods if p["period_start"] == OCT_START)
    assert [pl["retailer"] for pl in october["plans"]] == ["AGL Energy"]
    assert october["spans_switch"] is False


async def test_a_period_straddling_the_switch_is_flagged(client):
    """September contains the 9th. Its figures were snapshotted once, by
    whichever plan was current when it closed — so the total attributes Amber's
    first nine days to AGL. Flag it rather than present it as unambiguous."""
    await _backdate_opening_plan(client)
    await _seed_period(SEP_START, OCT_START)
    await client.post(
        "/api/tariff/plans", json={"valid_from": SWITCH, "retailer": "AGL Energy"}
    )

    periods = (await client.get("/api/tariff/history")).json()["periods"]

    september = next(p for p in periods if p["period_start"] == SEP_START)
    assert september["spans_switch"] is True
    assert len(september["plans"]) == 2


async def test_history_still_works_with_no_plans_at_all(client):
    """The annotation must not turn a working history endpoint into a 500 for a
    deployment whose service was never set up."""
    await _seed_period(OCT_START, ts(2026, 11, 1))

    resp = await client.get("/api/tariff/history?service=")

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["periods"]) == 1
