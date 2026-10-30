"""AEMO NEM wholesale spot price (#89 part A): parse, cache, failure, sensors, routes.

No network: the client's fetcher is injected, and the one test of the real
request builder swaps httpx's transport for a MockTransport.
"""

from __future__ import annotations

import calendar
from datetime import datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway import aemo_nem as nem
from franklinwh_bridge.gateway.aemo_nem import (
    NemPricingService,
    NemSpotClient,
    ha_price_c_kwh,
    parse_summary,
    resolve_prices,
    settlement_epoch,
)
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog, snapshot
from franklinwh_bridge.store.db import init_db


def _summary(nsw: float = 123.45, settlement: str = "2026-10-15T14:35:00") -> dict:
    return {
        "ELEC_NEM_SUMMARY": [
            {"REGIONID": "QLD1", "PRICE": 80.0, "SETTLEMENTDATE": settlement},
            {"REGIONID": "NSW1", "PRICE": nsw, "SETTLEMENTDATE": settlement},
            {"REGIONID": "SA1", "PRICE": -45.678, "SETTLEMENTDATE": settlement},
        ]
    }


class _Fetcher:
    def __init__(self, data: dict | None = None):
        self.data = data if data is not None else _summary()
        self.calls = 0
        self.fail = False

    async def __call__(self) -> dict:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("down")
        return self.data


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ── pure ──────────────────────────────────────────────────────


def test_parse_picks_region_and_converts_rrp_to_c_kwh():
    row = parse_summary(_summary(), "NSW1")
    assert row["region"] == "NSW1"
    assert row["rrp_mwh"] == 123.45
    assert row["price_c_kwh"] == 12.35  # RRP / 10, rounded to 2
    assert row["settlement"] == "2026-10-15T14:35:00"
    assert row["source"] == "AEMO NEM" and row["stale"] is False


def test_parse_negative_price_and_missing_region():
    assert parse_summary(_summary(), "SA1")["price_c_kwh"] == -4.57
    assert parse_summary(_summary(), "VIC1") is None
    assert parse_summary({}, "NSW1") is None
    assert (
        parse_summary({"ELEC_NEM_SUMMARY": [{"REGIONID": "NSW1", "PRICE": None}]}, "NSW1") is None
    )


def test_settlement_is_nem_time_utc_plus_10_no_dst():
    # 2026-10-15 is AEDT in Sydney, but NEM time stays AEST (UTC+10).
    expected = calendar.timegm(datetime(2026, 10, 15, 4, 35).timetuple())
    assert settlement_epoch("2026-10-15T14:35:00") == expected
    assert settlement_epoch(None) is None and settlement_epoch("bad") is None


def test_ha_price_units():
    assert ha_price_c_kwh({"state": "0.28", "attributes": {"unit_of_measurement": "$/kWh"}}) == 28.0
    assert (
        ha_price_c_kwh({"state": "0.3", "attributes": {"unit_of_measurement": "AUD/kWh"}}) == 30.0
    )
    assert ha_price_c_kwh({"state": "28", "attributes": {"unit_of_measurement": "c/kWh"}}) == 28.0
    assert ha_price_c_kwh({"state": "-5", "attributes": {}}) == -5.0
    assert ha_price_c_kwh({"state": "unavailable"}) is None
    assert ha_price_c_kwh(None) is None


def test_resolve_nem_then_ha_wins_per_side():
    spot = {"price_c_kwh": 10.0}
    assert resolve_prices(spot) == {
        "source": "nem",
        "buy_c_kwh": 10.0,
        "sell_c_kwh": 10.0,
        "sell_source": "nem",
    }
    r = resolve_prices(spot, ha_buy=30.0)
    assert (r["source"], r["buy_c_kwh"], r["sell_c_kwh"], r["sell_source"]) == (
        "ha-entity",
        30.0,
        10.0,
        "nem",
    )
    r = resolve_prices(None, ha_sell=5.0)
    assert r["buy_c_kwh"] is None and r["sell_c_kwh"] == 5.0 and r["source"] is None
    assert resolve_prices(None)["buy_c_kwh"] is None


# ── client cache / failure ────────────────────────────────────


async def test_cache_240s_shared_across_regions():
    f, clk = _Fetcher(), _Clock()
    c = NemSpotClient(fetcher=f, clock=clk)
    assert (await c.spot_price("nsw1"))["price_c_kwh"] == 12.35
    assert (await c.spot_price("QLD1"))["price_c_kwh"] == 8.0  # same response
    assert f.calls == 1
    clk.t += 239
    await c.spot_price("NSW1")
    assert f.calls == 1
    clk.t += 2
    await c.spot_price("NSW1")
    assert f.calls == 2


async def test_unknown_region_never_fetches():
    f = _Fetcher()
    c = NemSpotClient(fetcher=f)
    assert await c.spot_price("WA1") is None
    assert await c.spot_price("") is None
    assert f.calls == 0


async def test_failure_returns_stale_then_none_never_raises():
    f, clk = _Fetcher(), _Clock()
    c = NemSpotClient(fetcher=f, clock=clk)
    f.fail = True
    assert await c.spot_price("NSW1") is None
    f.fail = False
    await c.spot_price("NSW1")
    f.fail = True
    clk.t += 300
    out = await c.spot_price("NSW1")
    assert out["stale"] is True and out["price_c_kwh"] == 12.35


async def test_failure_with_no_text_still_says_why(caplog):
    """A timeout's text is empty: the log line named no reason at all."""
    async def timeout() -> dict:
        raise httpx.ReadTimeout("")

    caplog.set_level("WARNING", logger="franklinwh_bridge.gateway.aemo_nem")
    assert await NemSpotClient(fetcher=timeout).spot_price("NSW1") is None
    assert "AEMO NEM fetch failed: ReadTimeout" in caplog.text


async def test_region_missing_from_response_is_none():
    c = NemSpotClient(fetcher=_Fetcher(_summary()))
    assert await c.spot_price("TAS1") is None


async def test_cached_flags_stale_and_ages_out():
    f, clk = _Fetcher(), _Clock()
    c = NemSpotClient(fetcher=f, clock=clk)
    assert c.cached("NSW1") is None
    await c.spot_price("NSW1")
    assert c.cached("NSW1")["stale"] is False
    clk.t += 300
    assert c.cached("NSW1")["stale"] is True
    assert c.cached("NSW1", max_age_s=nem.SENSOR_MAX_AGE_S) is not None
    clk.t += nem.SENSOR_MAX_AGE_S
    assert c.cached("NSW1", max_age_s=nem.SENSOR_MAX_AGE_S) is None


async def test_real_fetcher_hits_aemo_and_parses(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json=_summary())

    real = httpx.AsyncClient

    def fake(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        assert kw["timeout"] == 12.0
        return real(*a, **kw)

    monkeypatch.setattr(nem.httpx, "AsyncClient", fake)
    c = NemSpotClient()
    assert (await c.spot_price("NSW1"))["price_c_kwh"] == 12.35
    assert seen == [nem.AEMO_URL]


async def test_real_fetcher_http_error_is_none(monkeypatch):
    real = httpx.AsyncClient

    def fake(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(503))
        return real(*a, **kw)

    monkeypatch.setattr(nem.httpx, "AsyncClient", fake)
    assert await NemSpotClient().spot_price("NSW1") is None


# ── service: settings + points ────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "t.db")
    yield conn
    await conn.close()


async def test_settings_default_off_persist_and_validate(db):
    svc = NemPricingService(db, client=NemSpotClient(fetcher=_Fetcher()))
    await svc.load()
    assert svc.values() == {"nem_region": "", "tariff_price_entity": "", "tariff_feedin_entity": ""}
    await svc.update({"nem_region": "vic1", "tariff_price_entity": "ha:home:sensor.price"})
    again = NemPricingService(db)
    await again.load()
    assert again.settings.nem_region == "VIC1"
    assert again.settings.tariff_price_entity == "ha:home:sensor.price"
    with pytest.raises(ValueError):
        await svc.update({"nem_region": "WA1"})
    with pytest.raises(ValueError):
        await svc.update({"tariff_feedin_entity": "sensor.no_prefix"})
    assert svc.settings.nem_region == "VIC1"  # unchanged by the rejected update
    await svc.update({"nem_region": ""})
    assert svc.settings.nem_region == ""


async def test_points_none_when_off_and_never_fetch(db):
    f = _Fetcher()
    svc = NemPricingService(db, client=NemSpotClient(fetcher=f))
    pts = svc.as_points()
    assert pts == {"tariff_spot_price": None, "tariff_feed_in_price": None}
    snap = snapshot(pts)
    assert snap["tariff.spot_price"] is None and snap["tariff.feed_in_price"] is None
    assert f.calls == 0


async def test_points_from_cache_nem_and_ha_override(db):
    f = _Fetcher()
    states = {
        "ha:home:sensor.feed": {"state": "0.05", "attributes": {"unit_of_measurement": "$/kWh"}}
    }
    svc = NemPricingService(db, client=NemSpotClient(fetcher=f), entity_state=states.get)
    await svc.update({"nem_region": "NSW1"})
    assert svc.as_points()["tariff_spot_price"] is None  # cache cold: no network here
    await svc.client.spot_price("NSW1")
    assert svc.as_points() == {"tariff_spot_price": 12.35, "tariff_feed_in_price": 12.35}
    await svc.update({"tariff_feedin_entity": "ha:home:sensor.feed"})
    snap = snapshot(svc.as_points())
    assert snap["tariff.spot_price"] == 12.35 and snap["tariff.feed_in_price"] == 5.0


def test_sensors_listed_with_units_and_buy_sell_rate_untouched():
    cat = {s["id"]: s for s in sensor_catalog()}
    assert cat["tariff.spot_price"]["unit"] == "c/kWh"
    assert cat["tariff.feed_in_price"]["unit"] == "c/kWh"
    assert cat["tariff.buy_rate"]["unit"] == "$/kWh"  # static rate model, part B
    assert cat["tariff.spot_price"]["group"] == "Tariff"


# ── routes ────────────────────────────────────────────────────


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


async def test_route_spot_off_fetches_nothing(client):
    body = (await client.get("/api/tariff/spot")).json()
    assert body["region"] is None and body["configured_region"] is None
    assert body["spot"] is None and body["regions"] == list(nem.REGIONS)
    assert body["active"]["source"] is None
    assert client.fetcher.calls == 0


async def test_route_settings_round_trip_and_spot(client):
    r = await client.put("/api/tariff/spot/settings", json={"nem_region": "NSW1"})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert (await client.get("/api/tariff/spot/settings")).json()["values"]["nem_region"] == "NSW1"
    body = (await client.get("/api/tariff/spot")).json()
    assert body["configured_region"] == "NSW1"
    assert body["spot"]["price_c_kwh"] == 12.35
    assert body["active"] == {
        "source": "nem",
        "buy_c_kwh": 12.35,
        "sell_c_kwh": 12.35,
        "sell_source": "nem",
    }
    # Ad-hoc region lookup does not change the active (configured) source.
    other = (await client.get("/api/tariff/spot?region=QLD1")).json()
    assert other["spot"]["price_c_kwh"] == 8.0 and other["active"]["buy_c_kwh"] == 12.35
    # Reaches the condition sensors via /api/sensors.
    r = await client.get("/api/sensors")
    if r.status_code != 200:  # automations module gate off in this env
        pytest.skip(f"/api/sensors unavailable: {r.status_code}")
    cat = {s["id"]: s for s in r.json()["sensors"]}
    assert cat["tariff.spot_price"]["value"] == 12.35


async def test_route_rejects_bad_region(client):
    assert (await client.get("/api/tariff/spot?region=WA1")).status_code == 400
    r = await client.put("/api/tariff/spot/settings", json={"nem_region": "WA1"})
    assert r.status_code == 422
    r = await client.put("/api/tariff/spot/settings", json={"tariff_price_entity": "x"})
    assert r.status_code == 422
