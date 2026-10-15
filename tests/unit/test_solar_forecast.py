"""Open-Meteo weather + solar forecast (#88): model, sensors, cache, failure, routes.

No network: the fetcher is injected, and the one test of the real request
builder swaps httpx's transport for a MockTransport.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway import solar_forecast as sf
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog, snapshot
from franklinwh_bridge.gateway.solar_forecast import (
    PvSettings,
    SolarForecastService,
    actual_vs_forecast,
    compute,
    sensors,
)
from franklinwh_bridge.store.db import init_db

SYD = 36000  # AEST, UTC+10


def _raw(offset: int = SYD, start: str = "2026-10-15") -> dict:
    """Two local days of hourly data; GTI is a triangle peaking at 12:00 local."""
    times, gti, temps = [], [], []
    for day in (start, _next_day(start)):
        for h in range(24):
            times.append(f"{day}T{h:02d}:00")
            gti.append(max(0, 1000 - abs(12 - h) * 200))  # 0 outside 08..16
            temps.append(15 + h / 2)
    gti[24 + 12] = 500  # tomorrow is cloudier at noon
    return {
        "utc_offset_seconds": offset,
        "timezone": "Australia/Sydney",
        "current": {"temperature_2m": 21.4, "weather_code": 3},
        "hourly": {"time": times, "global_tilted_irradiance": gti, "temperature_2m": temps},
    }


def _next_day(d: str) -> str:
    return datetime.fromtimestamp(
        calendar.timegm(datetime.strptime(d, "%Y-%m-%d").timetuple()) + 86400, UTC
    ).strftime("%Y-%m-%d")


def _epoch_at_site(local: str, offset: int = SYD) -> int:
    return calendar.timegm(datetime.strptime(local, "%Y-%m-%dT%H:%M").timetuple()) - offset


# ── kW model + day totals ─────────────────────────────────────


def test_kw_model_is_gti_over_1000_times_kwp_times_derate():
    fc = compute(_raw(), kwp=5.0)
    noon = next(h for h in fc["hourly"] if h["time"] == "2026-10-15T12:00")
    assert noon["kw"] == pytest.approx(1000 / 1000 * 5.0 * 0.85)  # 4.25
    night = next(h for h in fc["hourly"] if h["time"] == "2026-10-15T02:00")
    assert night["kw"] == 0


def test_hour_epoch_uses_site_offset():
    fc = compute(_raw(), kwp=5.0)
    noon = next(h for h in fc["hourly"] if h["time"] == "2026-10-15T12:00")
    # 12:00 AEST is 02:00 UTC.
    assert noon["ts"] == calendar.timegm((2026, 10, 15, 2, 0, 0))


def test_day_totals_and_peak():
    fc = compute(_raw(), kwp=5.0)
    today, tomorrow = fc["days"]
    # Triangle 200..1000..200 over 08..16 = 5000 W/m² h -> 5 x 5 x 0.85.
    assert today == {
        "date": "2026-10-15",
        "total_kwh": 21.2,
        "peak_kw": 4.25,
        "peak_time": "12:00",
        "peak_ts": _epoch_at_site("2026-10-15T12:00"),
    }
    assert tomorrow["date"] == "2026-10-16"
    assert tomorrow["peak_time"] in ("11:00", "13:00")
    assert tomorrow["peak_kw"] == pytest.approx(3.4)
    assert fc["current"] == {"temp_c": 21.4, "code": 3, "condition": "Overcast"}


def test_missing_gti_values_count_as_zero():
    raw = _raw()
    raw["hourly"]["global_tilted_irradiance"][12] = None
    fc = compute(raw, kwp=5.0)
    assert fc["hourly"][12]["kw"] == 0


# ── sensors ───────────────────────────────────────────────────


def test_remaining_kwh_uses_site_local_time_not_utc():
    fc = compute(_raw(), kwp=5.0)
    # 12:30 AEST — the 12:00 hour has started, 13:00..16:00 remain.
    now = _epoch_at_site("2026-10-15T12:30")
    s = sensors(fc, now)
    expected = round(sum((1000 - abs(12 - h) * 200) / 1000 * 5 * 0.85 for h in range(13, 17)), 1)
    assert s["solar_forecast.remaining_kwh"] == expected
    assert s["solar_forecast.today_kwh"] == 21.2
    assert s["solar_forecast.today_peak_kw"] == 4.25
    assert s["solar_forecast.tomorrow_kwh"] == fc["days"][1]["total_kwh"]
    assert s["weather.temp_c"] == 21.4
    assert s["weather.condition"] == "Overcast"
    assert s["weather.code"] == 3


def test_remaining_before_sunrise_is_whole_day_and_after_sunset_zero():
    fc = compute(_raw(), kwp=5.0)
    assert sensors(fc, _epoch_at_site("2026-10-15T05:00"))["solar_forecast.remaining_kwh"] == 21.2
    assert sensors(fc, _epoch_at_site("2026-10-15T20:00"))["solar_forecast.remaining_kwh"] == 0


def test_today_is_the_site_date_so_an_old_forecast_fails_closed():
    """Two days on, the cached forecast holds neither today nor tomorrow."""
    fc = compute(_raw(), kwp=5.0)
    s = sensors(fc, _epoch_at_site("2026-10-17T10:00"))
    assert s["solar_forecast.today_kwh"] is None
    assert s["solar_forecast.remaining_kwh"] is None
    assert s["solar_forecast.tomorrow_kwh"] is None
    # The next day, yesterday's "tomorrow" is today.
    s = sensors(fc, _epoch_at_site("2026-10-16T06:00"))
    assert s["solar_forecast.today_kwh"] == fc["days"][1]["total_kwh"]
    assert s["solar_forecast.tomorrow_kwh"] is None


def test_sensors_none_for_empty_or_error_forecast():
    for fc in (None, {}, sf.empty_forecast("boom")):
        s = sensors(fc, 0)
        assert all(v is None for v in s.values()), s


def test_actual_vs_forecast():
    out = actual_vs_forecast(10.0, 20.0, 5.0)
    assert out["solar_forecast.day_pct"] == 50.0
    assert out["solar_forecast.vs_expected_pct"] == pytest.approx(66.7)
    assert actual_vs_forecast(None, 20.0, 5.0) == {
        "solar_forecast.day_pct": None,
        "solar_forecast.vs_expected_pct": None,
    }
    # Nothing expected yet (before sunrise) -> no ratio, not a divide-by-zero.
    assert actual_vs_forecast(0.0, 20.0, 20.0)["solar_forecast.vs_expected_pct"] is None
    assert actual_vs_forecast(1.0, 0.0, 0.0)["solar_forecast.day_pct"] is None


def test_registry_sensors_none_when_off_and_listed_with_units():
    snap = snapshot({})
    ids = [
        "weather.temp_c",
        "weather.condition",
        "weather.code",
        "solar_forecast.today_kwh",
        "solar_forecast.today_peak_kw",
        "solar_forecast.tomorrow_kwh",
        "solar_forecast.remaining_kwh",
        "solar_forecast.day_pct",
        "solar_forecast.vs_expected_pct",
    ]
    for sid in ids:
        assert sid in snap and snap[sid] is None, sid
    cat = {c["id"]: c for c in sensor_catalog()}
    assert cat["solar_forecast.today_kwh"]["unit"] == "kWh"
    assert cat["solar_forecast.today_peak_kw"]["unit"] == "kW"
    assert cat["weather.temp_c"]["unit"] == "°C"
    assert cat["solar_forecast.day_pct"]["unit"] == "%"
    assert cat["weather.condition"]["kind"] == "enum"
    assert cat["weather.temp_c"]["group"] == "Weather"
    assert cat["solar_forecast.remaining_kwh"]["group"] == "Solar Forecast"


def test_registry_derives_actual_vs_forecast_from_energy_today():
    snap = snapshot(
        {
            "energy_solar_today_kwh": 6.0,
            "solar_forecast_today_kwh": 20.0,
            "solar_forecast_remaining_kwh": 8.0,
        }
    )
    assert snap["solar_forecast.day_pct"] == 30.0
    assert snap["solar_forecast.vs_expected_pct"] == 50.0
    assert snap["solar_forecast.today_kwh"] == 20.0


# ── service: settings, cache, failure ─────────────────────────


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "s.db")
    yield conn
    await conn.close()


class _Fetcher:
    def __init__(self, raw: dict | None = None):
        self.raw = raw or _raw()
        self.calls: list[PvSettings] = []
        self.fail: Exception | None = None

    async def __call__(self, s: PvSettings) -> dict:
        self.calls.append(s)
        if self.fail:
            raise self.fail
        return self.raw


async def test_settings_defaults_persist_and_clear(db):
    svc = SolarForecastService(db, fetcher=_Fetcher())
    await svc.load()
    assert svc.values() == {
        "pv_latitude": None,
        "pv_longitude": None,
        "pv_kwp": 5.0,
        "pv_tilt": 20.0,
        "pv_azimuth": 0.0,
    }
    assert not svc.settings.configured
    await svc.update({"pv_latitude": 12.34, "pv_longitude": 56.78, "pv_azimuth": -90})
    again = SolarForecastService(db)
    await again.load()
    assert again.settings.configured
    assert again.values()["pv_azimuth"] == -90.0
    await again.update({"pv_latitude": None})
    assert not again.settings.configured


async def test_settings_reject_out_of_range(db):
    svc = SolarForecastService(db)
    with pytest.raises(ValueError):
        await svc.update({"pv_latitude": 91})
    with pytest.raises(ValueError):
        await svc.update({"pv_kwp": None})
    assert svc.values()["pv_latitude"] is None


async def test_not_configured_never_fetches(db):
    f = _Fetcher()
    svc = SolarForecastService(db, fetcher=f)
    fc = await svc.forecast()
    assert f.calls == []
    assert fc["days"] == [] and "error" in fc
    assert all(v is None for v in svc.as_points().values())


async def test_cache_900s_keyed_on_site_and_array(db, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(sf.time, "time", lambda: clock[0])
    f = _Fetcher()
    svc = SolarForecastService(db, fetcher=f)
    await svc.update({"pv_latitude": 12.34, "pv_longitude": 56.78})
    await svc.forecast()
    await svc.forecast()
    assert len(f.calls) == 1
    clock[0] += 899
    await svc.forecast()
    assert len(f.calls) == 1
    await svc.forecast(force=True)
    assert len(f.calls) == 2
    clock[0] += 901
    await svc.forecast()
    assert len(f.calls) == 3
    # Any key field changing is a different forecast.
    for change in (
        {"pv_kwp": 6.6},
        {"pv_tilt": 30},
        {"pv_azimuth": 90},
        {"pv_latitude": -34.0},
        {"pv_longitude": 150.0},
    ):
        n = len(f.calls)
        await svc.update(change)
        await svc.forecast()
        assert len(f.calls) == n + 1, change
    assert f.calls[-1].pv_longitude == 150.0


async def test_failure_returns_stub_then_stale_cache_never_raises(db, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(sf.time, "time", lambda: clock[0])
    f = _Fetcher()
    f.fail = httpx.ConnectTimeout("timed out")
    svc = SolarForecastService(db, fetcher=f)
    await svc.update({"pv_latitude": 12.34, "pv_longitude": 56.78})
    fc = await svc.forecast()
    assert fc["error"] == "timed out" and fc["days"] == []
    assert "stale" not in fc

    f.fail = None
    good = await svc.forecast()
    assert "stale" not in good and good["days"]

    f.fail = ValueError("bad json")
    clock[0] += 1000
    stale = await svc.forecast()
    assert stale["stale"] is True
    assert stale["days"] == good["days"]


async def test_as_points_reads_cache_only(db, monkeypatch):
    f = _Fetcher()
    svc = SolarForecastService(db, fetcher=f)
    await svc.update({"pv_latitude": 12.34, "pv_longitude": 56.78})
    assert all(v is None for v in svc.as_points().values())  # nothing fetched yet
    await svc.forecast()
    pts = svc.as_points(_epoch_at_site("2026-10-15T12:30"))
    assert pts["solar_forecast_today_kwh"] == 21.2
    assert pts["weather_condition"] == "Overcast"
    assert pts["solar_forecast_remaining_kwh"] is not None
    assert len(f.calls) == 1
    snap = snapshot(pts)
    assert snap["solar_forecast.today_kwh"] == 21.2


async def test_real_fetcher_sends_open_meteo_params(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = req.url
        return httpx.Response(200, json=_raw())

    real = httpx.AsyncClient
    monkeypatch.setattr(
        sf.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    raw = await sf.fetch_open_meteo(PvSettings(12.34, 56.78, 6.6, 22, -90))
    assert raw["utc_offset_seconds"] == SYD
    q = dict(seen["url"].params)
    assert str(seen["url"]).startswith(sf.OPEN_METEO)
    assert q == {
        "latitude": "12.34",
        "longitude": "56.78",
        "timezone": "auto",
        "forecast_days": "2",
        "current": "temperature_2m,weather_code",
        "hourly": "temperature_2m,global_tilted_irradiance",
        "tilt": "22",
        "azimuth": "-90",
    }


async def test_real_fetcher_http_error_becomes_stub(db, monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        sf.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw),
    )
    svc = SolarForecastService(db)
    await svc.update({"pv_latitude": 12.34, "pv_longitude": 56.78})
    fc = await svc.forecast()
    assert "503" in fc["error"]


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
        fetcher = _Fetcher()
        app.state.solar_forecast._fetch = fetcher
        ac.fetcher = fetcher
        ac.app = app
        yield ac


async def test_route_forecast_unconfigured_gives_reason(client):
    body = (await client.get("/api/solar/forecast")).json()
    assert body == {"configured": False, "reason": sf.NOT_CONFIGURED_REASON}
    assert client.fetcher.calls == []


async def test_route_settings_round_trip_and_forecast(client):
    r = await client.get("/api/solar/settings")
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert "0 = south" in r.json()["azimuth_help"]

    r = await client.put(
        "/api/solar/settings", json={"pv_latitude": 12.34, "pv_longitude": 56.78, "pv_kwp": 6.6}
    )
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert r.json()["values"]["pv_kwp"] == 6.6
    assert r.json()["values"]["pv_tilt"] == 20.0  # untouched

    body = (await client.get("/api/solar/forecast")).json()
    assert body["configured"] is True
    assert body["pv"] == {"kwp": 6.6, "tilt": 20.0, "azimuth": 0.0, "lat": 12.34, "lon": 56.78}
    assert len(body["days"]) == 2 and len(body["hourly"]) == 48
    assert "solar_forecast.remaining_kwh" in body["sensors"]
    assert "solar_forecast.day_pct" in body["sensors"]
    n = len(client.fetcher.calls)
    await client.get("/api/solar/forecast?force=true")
    assert len(client.fetcher.calls) == n + 1

    # Off again.
    r = await client.put("/api/solar/settings", json={"pv_latitude": None})
    assert r.json()["configured"] is False
    assert (await client.get("/api/solar/forecast")).json()["configured"] is False


async def test_route_rejects_bad_values(client):
    r = await client.put("/api/solar/settings", json={"pv_latitude": 123})
    assert r.status_code == 422
    r = await client.put("/api/solar/settings", json={"pv_azimuth": 200})
    assert r.status_code == 422
    r = await client.put("/api/solar/settings", json={"pv_kwp": 0})
    assert r.status_code == 422


async def test_route_forecast_failure_is_200_with_error(client):
    client.fetcher.fail = httpx.ConnectError("no route")
    await client.put("/api/solar/settings", json={"pv_latitude": 1.0, "pv_longitude": 2.0})
    r = await client.get("/api/solar/forecast?force=1")
    assert r.status_code == 200
    assert r.json()["error"] == "no route"
    assert r.json()["sensors"]["solar_forecast.today_kwh"] is None


async def test_route_from_ha_without_instance_is_404(client):
    r = await client.post("/api/solar/location/from-ha")
    assert r.status_code == 404


async def test_route_from_ha_uses_get_config(client):
    class _Inst:
        async def ws_call(self, cmd):
            assert cmd == {"type": "get_config"}
            return {"latitude": 12.34, "longitude": 56.78, "elevation": 30}

    class _Reg:
        def status(self):
            return [{"id": "ha1", "name": "Home"}]

        def get(self, iid):
            return _Inst() if iid == "ha1" else None

    real = client.app.state.ha_registry
    client.app.state.ha_registry = _Reg()
    try:
        s = (await client.get("/api/solar/settings")).json()
        assert s["ha_instances"] == [{"id": "ha1", "name": "Home"}]
        r = await client.post("/api/solar/location/from-ha")
        assert r.status_code == 200, r.text
        assert r.json()["values"]["pv_latitude"] == 12.34
        assert r.json()["configured"] is True
    finally:
        client.app.state.ha_registry = real


async def test_condition_sensors_reach_api_sensors(client):
    await client.put("/api/solar/settings", json={"pv_latitude": 12.34, "pv_longitude": 56.78})
    await client.get("/api/solar/forecast")  # warms the cache
    r = await client.get("/api/sensors")
    if r.status_code != 200:  # automations module gate off in this env
        pytest.skip(f"/api/sensors unavailable: {r.status_code}")
    cat = {s["id"]: s for s in r.json()["sensors"]}
    assert "solar_forecast.tomorrow_kwh" in cat
    assert cat["weather.condition"]["value"] == "Overcast"
