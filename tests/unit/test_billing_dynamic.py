"""Dynamic (live) tariff in billing (#89 part B).

A service whose ``pricing.kind`` is "dynamic" prices its energy at the live
NEM spot or HA price instead of the static seasons. Mirrors the Local Bridge's
``tests/test_billing_dynamic.py``; no network anywhere (the NEM fetcher and
HA entity states are injected).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway import dynamic_pricing as dp
from franklinwh_bridge.gateway.aemo_nem import NemPricingService, NemSpotClient
from franklinwh_bridge.gateway.billing import BillingStore
from franklinwh_bridge.gateway.demand import DemandTracker
from franklinwh_bridge.gateway.scheduler_sensors import snapshot
from franklinwh_bridge.store.db import (
    get_billing_periods,
    init_db,
    update_service,
)

STATIC = {"default_rate": {"buy": 0.30, "sell": 0.05}}
NEM_PLAN = {**STATIC, "kind": "dynamic", "dynamic": {"source": "nem", "region": "NSW1"}}
HA_BUY = "ha:home:sensor.amber_general_price"
HA_SELL = "ha:home:sensor.amber_feed_in_price"


class FakeBilling:
    """Only what the tracker asks of it."""

    def __init__(self, pricing: dict):
        self.pricing = pricing

    def as_points(self):
        return {"tariff_default_rate": self.pricing.get("default_rate")}

    def dynamic_config(self):
        return self.pricing.get("dynamic") if dp.is_dynamic(self.pricing) else None

    def demand_config(self):
        return None

    def bonus_config(self):
        return None

    def charge_config(self):
        return None

    def plan(self):
        return {}


class LivePrice:
    """A stand-in for NemPricingService.dynamic_prices: {buy, sell} $/kWh."""

    def __init__(self, buy_c: float | None, sell_c: float | None = None):
        self.buy_c, self.sell_c = buy_c, sell_c

    def __call__(self, dyn: dict) -> dict:
        spot = None if self.buy_c is None else {"price_c_kwh": self.buy_c}
        if dyn.get("source") == "nem":
            return dp.live_prices(dyn, spot)
        states = {}
        if self.buy_c is not None:
            states[dyn["buy_entity"]] = {"state": str(self.buy_c), "attributes": {}}
        if self.sell_c is not None and dyn.get("feedin_entity"):
            states[dyn["feedin_entity"]] = {"state": str(self.sell_c), "attributes": {}}
        return dp.live_prices(dyn, None, states.get)


def sample(ts: dt.datetime, import_wh: float, export_wh: float = 0.0):
    return SimpleNamespace(
        gateway_id="default",
        ts=ts.timestamp(),
        points={"grid_import_wh": import_wh, "grid_export_wh": export_wh},
    )


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "dyn.db")
    yield conn
    await conn.close()


def _tracker(db, pricing, live=None):
    return DemandTracker(db, FakeBilling(pricing), gateway_id="default", live_prices=live)


T0 = dt.datetime(2026, 9, 30, 12, 0, 0)


# ── pure: validation + rate ───────────────────────────────────


def test_validation_matches_the_local_bridge():
    assert dp.normalise_dynamic({"source": "nem", "region": "nsw1"}) == {
        "source": "nem",
        "region": "NSW1",
    }
    assert dp.normalise_dynamic({"source": "ha", "buy_entity": HA_BUY}) == {
        "source": "ha",
        "buy_entity": HA_BUY,
        "feedin_entity": "",
    }
    for bad, msg in (
        ({"source": "nem"}, "NEM region"),
        ({"source": "nem", "region": "WA1"}, "region must be one of"),
        ({"source": "ha"}, "buy price entity"),
        ({"source": "ha", "buy_entity": "sensor.x"}, "ha:<instance>"),
        ({"source": "ha", "buy_entity": HA_BUY, "feedin_entity": "nope"}, "feedin_entity"),
        ({"source": "amber"}, "provider"),
        (None, "provider"),
    ):
        with pytest.raises(ValueError, match=msg):
            dp.normalise_dynamic(bad)
    with pytest.raises(ValueError, match="static' or 'dynamic"):
        dp.normalise_pricing({"kind": "spot"})
    # Static pricing (with or without a kind) passes through untouched.
    assert dp.normalise_pricing(STATIC) is STATIC
    assert dp.normalise_pricing({**STATIC, "kind": "static"})["kind"] == "static"
    assert dp.pricing_problems(STATIC) is None
    assert dp.pricing_problems(NEM_PLAN) == []
    assert dp.pricing_problems({"kind": "dynamic", "dynamic": {"source": "nem"}})


def test_nem_sell_equals_buy_and_cents_to_dollars():
    got = dp.live_prices({"source": "nem", "region": "NSW1"}, {"price_c_kwh": 12.4})
    assert got["buy_c_kwh"] == got["sell_c_kwh"] == 12.4
    assert got["buy"] == got["sell"] == pytest.approx(0.124)
    assert got["label"] == "Wholesale (live) · AEMO NEM NSW1"
    none = dp.live_prices({"source": "nem", "region": "NSW1"}, None)
    assert none["buy"] is None and none["sell"] is None


def test_ha_feed_in_falls_back_to_buy():
    dyn = {"source": "ha", "buy_entity": HA_BUY, "feedin_entity": HA_SELL}
    both = LivePrice(30.0, 8.0)(dyn)
    assert both["buy"] == pytest.approx(0.30) and both["sell"] == pytest.approx(0.08)
    only_buy = LivePrice(30.0, None)(dyn)
    assert only_buy["sell"] == pytest.approx(0.30) and only_buy["sell_c_kwh"] == 30.0
    no_feedin = LivePrice(30.0, 8.0)({**dyn, "feedin_entity": ""})
    assert no_feedin["sell"] == pytest.approx(0.30)
    # A $/kWh unit is scaled by part A's helper.
    states = {HA_BUY: {"state": "0.25", "attributes": {"unit_of_measurement": "$/kWh"}}}
    assert dp.live_prices({**dyn, "feedin_entity": ""}, None, states.get)["buy"] == 0.25


def test_as_rate_shape():
    r = dp.as_rate({"buy": 0.12, "sell": 0.06, "label": "x"})
    assert r["reason"] == "dynamic" and r["billable"] is True and r["season"] is None
    assert dp.as_rate({"buy": -0.02, "sell": -0.02})["billable"] is False
    assert dp.as_rate(None)["buy"] is None and dp.as_rate(None)["reason"] == "no_live_price"


# ── accrual ───────────────────────────────────────────────────


async def test_dynamic_overrides_static_rate_for_import(db):
    t = _tracker(db, NEM_PLAN, LivePrice(10.0))
    await t.on_sample(sample(T0, 1000.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 4600.0))  # 3.6 kWh
    pts = t.as_points(now=T0 + dt.timedelta(minutes=5))
    # 3.6 kWh x $0.10 live, not the $0.30 static rate
    assert pts["energy_import_cost"] == pytest.approx(0.36)
    assert pts["energy_unpriced_import_kwh"] == 0.0


async def test_export_credited_at_live_sell(db):
    dyn = {
        **STATIC,
        "kind": "dynamic",
        "dynamic": {"source": "ha", "buy_entity": HA_BUY, "feedin_entity": HA_SELL},
    }
    t = _tracker(db, dyn, LivePrice(30.0, 8.0))
    await t.on_sample(sample(T0, 0.0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=10), 0.0, 2000.0))  # 2 kWh out
    pts = t.as_points(now=T0 + dt.timedelta(minutes=10))
    assert pts["energy_export_credit"] == pytest.approx(0.16)  # 2 x $0.08


async def test_nem_export_credited_at_spot(db):
    t = _tracker(db, NEM_PLAN, LivePrice(-5.0))  # negative spot: export costs
    await t.on_sample(sample(T0, 0.0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 1000.0, 2000.0))
    pts = t.as_points(now=T0 + dt.timedelta(minutes=5))
    assert pts["energy_import_cost"] == pytest.approx(-0.05)
    assert pts["energy_export_credit"] == pytest.approx(-0.10)


async def test_ha_without_feed_in_credits_export_at_buy(db):
    dyn = {**STATIC, "kind": "dynamic", "dynamic": {"source": "ha", "buy_entity": HA_BUY}}
    t = _tracker(db, dyn, LivePrice(20.0))
    await t.on_sample(sample(T0, 0.0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 0.0, 1000.0))
    assert t.as_points(now=T0)["energy_export_credit"] == pytest.approx(0.20)


async def test_no_live_price_goes_to_unpriced(db):
    t = _tracker(db, NEM_PLAN, LivePrice(None))
    await t.on_sample(sample(T0, 1000.0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 3000.0, 500.0))
    pts = t.as_points(now=T0)
    # Neither the stale/static $0.30 nor $0: not priced at all.
    assert pts["energy_import_cost"] == 0.0 and pts["energy_export_credit"] == 0.0
    assert pts["energy_unpriced_import_kwh"] == pytest.approx(2.0)
    assert pts["energy_unpriced_export_kwh"] == pytest.approx(0.5)
    # Still counted at the meter (tiers / period totals).
    assert pts["energy_period_import_kwh"] == pytest.approx(2.0)


async def test_unwired_live_source_prices_nothing(db):
    t = _tracker(db, NEM_PLAN, None)
    await t.on_sample(sample(T0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 1000.0))
    assert t.as_points(now=T0)["energy_unpriced_import_kwh"] == pytest.approx(1.0)


async def test_gap_over_an_hour_is_not_priced(db):
    t = _tracker(db, NEM_PLAN, LivePrice(10.0))
    await t.on_sample(sample(T0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=61), 5000.0))  # 61-min slice
    pts = t.as_points(now=T0)
    assert pts["energy_import_cost"] == 0.0
    assert pts["energy_unpriced_import_kwh"] == pytest.approx(5.0)
    # The next normal slice is priced again.
    await t.on_sample(sample(T0 + dt.timedelta(minutes=66), 6000.0))
    assert t.as_points(now=T0)["energy_import_cost"] == pytest.approx(0.10)
    # Exactly an hour is still priced.
    await t.on_sample(sample(T0 + dt.timedelta(minutes=126), 7000.0))
    assert t.as_points(now=T0)["energy_import_cost"] == pytest.approx(0.20)


async def test_static_plan_unchanged(db):
    """Regression: a static plan prices at its rate and ignores the gap rule."""
    t = _tracker(db, STATIC, LivePrice(10.0))
    await t.on_sample(sample(T0, 0.0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(hours=3), 2000.0, 1000.0))
    pts = t.as_points(now=T0)
    assert pts["energy_import_cost"] == pytest.approx(0.60)
    assert pts["energy_export_credit"] == pytest.approx(0.05)
    assert pts["energy_unpriced_import_kwh"] == 0.0
    assert t._prices_export() is True  # rate model still wins over the bonus window


async def test_dynamic_keeps_the_bonus_on_top(db):
    t = _tracker(db, NEM_PLAN, LivePrice(10.0))
    assert t._prices_export() is False


async def test_closed_period_records_pricing_and_unpriced(db):
    t = _tracker(db, NEM_PLAN, LivePrice(10.0))
    await t.on_sample(sample(T0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 1000.0))
    t._live_prices = LivePrice(None)
    await t.on_sample(sample(T0 + dt.timedelta(minutes=10), 1500.0))
    # Dynamic-only plan: no demand/bonus/charge/fixed, but still recorded.
    assert await t.close_period_now(T0 + dt.timedelta(minutes=11)) is True
    (row,) = await get_billing_periods(db)
    assert row["pricing_kind"] == "dynamic"
    assert row["pricing_label"] == "Wholesale (live) · AEMO NEM NSW1"
    assert row["unpriced_import_kwh"] == pytest.approx(0.5)
    assert row["energy_cost"] == pytest.approx(0.10)
    assert row["net_total"] == pytest.approx(0.10)


async def test_period_switched_mid_way_is_mixed(db):
    billing = FakeBilling(STATIC)
    t = DemandTracker(db, billing, gateway_id="default", live_prices=LivePrice(10.0))
    await t.on_sample(sample(T0, 0.0))
    await t.on_sample(sample(T0 + dt.timedelta(minutes=5), 1000.0))  # static
    billing.pricing = NEM_PLAN
    await t.on_sample(sample(T0 + dt.timedelta(minutes=10), 2000.0))  # live
    await t.close_period_now(T0 + dt.timedelta(minutes=11))
    (row,) = await get_billing_periods(db)
    assert row["pricing_kind"] == "mixed"
    assert row["pricing_label"].startswith("Static + Wholesale (live)")
    assert row["energy_cost"] == pytest.approx(0.40)


# ── BillingStore + sensors ────────────────────────────────────


async def test_billing_store_reads_kind(db):
    svc = {"id": "service1"}  # seeded by the migrations; the plan service
    store = BillingStore(db)
    await update_service(db, svc["id"], pricing=STATIC)
    await store.load()
    assert store.dynamic_config() is None and store.as_points()["tariff_pricing_kind"] == "static"
    await update_service(db, svc["id"], pricing=NEM_PLAN)
    await store.load()
    assert store.dynamic_config() == {"source": "nem", "region": "NSW1"}
    assert store.as_points()["tariff_pricing_kind"] == "dynamic"


class _Fetcher:
    def __init__(self, nsw: float = 123.4):
        self.nsw, self.calls = nsw, 0

    async def __call__(self):
        self.calls += 1
        return {
            "ELEC_NEM_SUMMARY": [
                {"REGIONID": "NSW1", "PRICE": self.nsw, "SETTLEMENTDATE": "2026-10-15T14:35:00"},
                {"REGIONID": "VIC1", "PRICE": 50.0, "SETTLEMENTDATE": "2026-10-15T14:35:00"},
            ]
        }


async def test_sensors_override_only_when_dynamic(db):
    svc = {"id": "service1"}  # seeded by the migrations; the plan service
    await update_service(db, svc["id"], pricing=STATIC)
    store = BillingStore(db)
    await store.load()
    nem = NemPricingService(db, client=NemSpotClient(fetcher=_Fetcher()))
    nem.set_plan_source(store.dynamic_config)
    await nem.update({"nem_region": "VIC1"})
    await nem.client.spot_price("VIC1")

    def snap():
        return snapshot({**store.as_points(), **nem.as_points()}, T0)

    s = snap()  # static: today's behaviour
    assert s["tariff.buy_rate"] == 0.30 and s["tariff.sell_rate"] == 0.05
    assert s["tariff.spot_price"] == 5.0  # the settings' region (VIC1)

    await update_service(db, svc["id"], pricing=NEM_PLAN)
    await store.load()
    s = snap()  # dynamic NSW1 plan wins over the VIC1 setting
    assert s["tariff.spot_price"] == s["tariff.feed_in_price"] == 12.34
    assert s["tariff.buy_rate"] == s["tariff.sell_rate"] == pytest.approx(0.1234)
    assert s["tariff.time_period"] == "Wholesale (live) · AEMO NEM NSW1"
    assert s["tariff.season"] is None and s["tariff.import_billable"] is True


async def test_sensors_none_when_live_price_too_old(db):
    clock = [1_000_000.0]
    client = NemSpotClient(fetcher=_Fetcher(), clock=lambda: clock[0])
    store = SimpleNamespace(dynamic_config=lambda: {"source": "nem", "region": "NSW1"})
    nem = NemPricingService(db, client=client)
    nem.set_plan_source(store.dynamic_config)
    await client.spot_price("NSW1")
    assert nem.plan_prices()["buy"] == pytest.approx(0.1234)
    assert nem.wanted_region() == "NSW1"  # the plan's region is kept warm
    clock[0] += dp.BILLING_MAX_AGE_S + 1
    assert nem.plan_prices()["buy"] is None  # stale -> prices nothing
    pts = nem.as_points()
    assert pts["tariff_dynamic_buy"] is None
    assert pts["tariff_spot_price"] == 12.34  # part A's 30-min sensor age still shows it


# ── REST ──────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from franklinwh_bridge.main import app

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        f = _Fetcher()
        app.state.nem_pricing.client._fetch = f
        ac.fetcher = f
        yield ac


async def test_service_patch_validates_and_applies(client):
    from franklinwh_bridge.main import app

    sid = "service1"  # the seeded, first (plan) service
    bad = await client.patch(
        f"/api/services/{sid}",
        json={"pricing": {"kind": "dynamic", "dynamic": {"source": "nem", "region": "XX"}}},
    )
    assert bad.status_code == 422
    assert "region must be one of" in bad.text
    ok = await client.patch(f"/api/services/{sid}", json={"pricing": NEM_PLAN})
    assert ok.status_code == 200, ok.text
    assert app.state.billing.dynamic_config() == {"source": "nem", "region": "NSW1"}

    body = (await client.get("/api/tariff/dynamic")).json()
    assert body["kind"] == "dynamic" and body["dynamic"]["region"] == "NSW1"
    assert "Demand" in body["note"]


async def test_dynamic_preview_and_validate_route(client):
    r = await client.get("/api/tariff/dynamic", params={"source": "nem", "region": "NSW1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"] is True and body["kind"] == "static"
    assert body["live"]["buy_c_kwh"] == body["live"]["sell_c_kwh"] == 12.34
    assert body["live"]["label"] == "Wholesale (live) · AEMO NEM NSW1"
    assert client.fetcher.calls == 1
    assert (await client.get("/api/tariff/dynamic", params={"source": "ha"})).status_code == 422
    assert (await client.get("/api/tariff/dynamic")).json()["live"] is None  # static plan

    v = await client.post(
        "/api/tariff/validate-rates", json={"kind": "dynamic", "dynamic": {"source": "ha"}}
    )
    assert v.json()["ok"] is False and "buy price entity" in v.json()["problems"][0]
    v = await client.post(
        "/api/tariff/validate-rates",
        json={"kind": "dynamic", "dynamic": {"source": "nem", "region": "SA1"}},
    )
    assert v.json()["ok"] is True


async def test_energy_costs_overview_and_history_show_pricing(client):
    from franklinwh_bridge.main import app
    from franklinwh_bridge.store.db import insert_billing_period

    assert (await client.patch("/api/modules/energy_costs", json={"enabled": True})).is_success
    static = (await client.get("/api/tariff/overview")).json()["pricing"]
    assert static["kind"] == "static" and static["buy_rate"] is None
    await client.patch("/api/services/service1", json={"pricing": NEM_PLAN})
    await app.state.nem_pricing.client.spot_price("NSW1")
    ov = (await client.get("/api/tariff/overview")).json()
    assert ov["pricing"]["kind"] == "dynamic"
    assert ov["pricing"]["label"] == "Wholesale (live) · AEMO NEM NSW1"
    assert ov["pricing"]["buy_rate"] == pytest.approx(0.1234)
    assert next(s for s in ov["services"] if s["id"] == "service1")["dynamic"] is True

    await insert_billing_period(
        app.state.db,
        {
            "gateway_id": "default",
            "period_start": 1.0e9,
            "period_end": 1.0e9 + 86400,
            "net_total": 1.0,
            "created_at": 1.0e9,
            "pricing_kind": "dynamic",
            "pricing_label": "Wholesale (live) · AEMO NEM NSW1",
            "unpriced_import_kwh": 0.5,
        },
    )
    (row,) = (await client.get("/api/tariff/history")).json()["periods"]
    assert row["pricing_kind"] == "dynamic" and row["unpriced_import_kwh"] == 0.5
    csv = (await client.get("/api/tariff/history.csv")).text
    assert "pricing_label" in csv.splitlines()[0] and "AEMO NEM NSW1" in csv


async def test_dynamic_profile_import_validates_provider(client):
    bundle = {
        "type": "franklinwh-bridge/tariff-profile",
        "version": 1,
        "profile": {"name": "Amber", "pricing": NEM_PLAN},
    }
    r = await client.post("/api/services/import", json=bundle)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bundle["profile"]["pricing"] = {"kind": "dynamic", "dynamic": {"source": "nem"}}
    r = await client.post("/api/services/import", json=bundle)
    assert r.json()["ok"] is False


@pytest.fixture
async def gated_client(tmp_path, monkeypatch):
    """Real auth (no conftest bypass), so roles are enforced."""
    from franklinwh_bridge.api.auth import get_current_user, require_auth
    from franklinwh_bridge.main import app

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        app.state.nem_pricing.client._fetch = _Fetcher()
        yield ac


@pytest.mark.parametrize("role", ["viewer", "user", "api"])
async def test_dynamic_writes_are_admin_only(gated_client, role):
    from franklinwh_bridge.main import app
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user

    svc = {"id": "service1"}
    await create_user(
        app.state.db, username=f"d_{role}", password_hash=hash_password("pw12345678"), role=role
    )
    r = await gated_client.post(
        "/api/auth/login", json={"username": f"d_{role}", "password": "pw12345678"}
    )
    assert r.status_code == 200, r.text
    r = await gated_client.patch(f"/api/services/{svc['id']}", json={"pricing": NEM_PLAN})
    assert r.status_code == 403
    assert app.state.billing.dynamic_config() is None
    # Reading the live price is open to every signed-in role.
    assert (await gated_client.get("/api/tariff/dynamic")).status_code == 200
