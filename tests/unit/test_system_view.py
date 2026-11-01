"""GET /api/system: the System view payload (DC bridge #428), from cached samples only."""

from pathlib import Path
from types import SimpleNamespace

import franklinwh_system_view as sv
import pytest

from franklinwh_bridge.api import system_view_api as svapi
from franklinwh_bridge.modbus.sample import Sample, SampleBus

TPL = Path(svapi.__file__).parents[1] / "templates"

POINTS = {
    "soc": 62.4, "battery_power_w": -2900, "grid_power_w": 300, "home_load_ext": 1300,
    "voltage_v": 240.2, "frequency_hz": 60.0, "voltage_l1_v": 120.1, "voltage_l2_v": 120.1,
    "connection_state": "Connected", "grid_mode": "Grid Following",
    "mode_name": "TOU", "inverter_state": "Running", "active_reserve_pct": 20,
    "pv_proximal": 4200, "pv_remote1": 0, "pv_remote2": 0, "total_solar": 4200,
    "battery_port_count": 2, "battery_1_power_w": -1500, "battery_2_power_w": -1400,
    "ac_type_code": 1,
}


@pytest.fixture(autouse=True)
def _fresh_seen():
    svapi._seen_pv.clear()


async def _inst(points, *, connected=True, quality="ok"):
    bus = SampleBus()
    await bus.publish(Sample.now("gw1", points, quality))
    return SimpleNamespace(sample_bus=bus, status=SimpleNamespace(connected=connected, ac_type=1),
                           poller=SimpleNamespace(battery_port_count=1))


ROW = {"id": "gw1", "name": "Garage", "enabled": 1}


async def test_values_as_read_and_valid_for_the_package():
    g = svapi.gateway_payload(ROW, await _inst(POINTS))
    assert g["online"] is True and g["name"] == "Garage"
    assert g["mode"] == "TOU" and g["inverter"] == "Running" and g["reserve_pct"] == 20
    assert g["grid"] == {"state": "connected", "power_w": 300, "voltage_v": 240.2,
                         "freq_hz": 60.0, "legs_v": [120.1, 120.1]}
    assert g["home"] == {"power_w": 1300}
    assert g["battery"]["soc_pct"] == 62.4 and g["battery"]["power_w"] == -2900
    assert [u["power_w"] for u in g["battery"]["units"]] == [-1500, -1400]
    # Only the input that produces is drawn; remote inputs at 0 W are not.
    assert g["solar"] == [{"id": "pv_proximal", "where": "agate", "name": "Solar (aGate)",
                           "power_w": 4200}]
    # Smart circuits and the generator have no Modbus points.
    assert g["circuits"] == [] and g["unavailable"] == ["circuits", "generator"]
    assert sv.validate({"version": 1, "gateways": [g]}) == []


async def test_grid_outage_from_connst():
    g = svapi.gateway_payload(ROW, await _inst({**POINTS, "connection_state": "Disconnected"}))
    assert g["grid"]["state"] == "outage"


async def test_grid_forming_alone_is_not_an_outage_while_connst_says_connected():
    g = svapi.gateway_payload(ROW, await _inst({**POINTS, "grid_mode": "Grid Forming"}))
    assert g["grid"]["state"] == "connected"


async def test_solar_stays_drawn_at_night_once_seen():
    inst = await _inst(POINTS)
    svapi.gateway_payload(ROW, inst)
    night = await _inst({**POINTS, "pv_proximal": 0, "total_solar": 0})
    assert [s["id"] for s in svapi.gateway_payload(ROW, night)["solar"]] == ["pv_proximal"]


async def test_no_solar_drawn_for_a_site_without_solar():
    pts = {**POINTS, "pv_proximal": 0, "total_solar": 0, "pv_energy_total_wh": 0}
    assert svapi.gateway_payload(ROW, await _inst(pts))["solar"] == []


async def test_offline_when_disconnected_or_error_or_stopped():
    for inst in (await _inst(POINTS, connected=False), await _inst(POINTS, quality="error"), None):
        g = svapi.gateway_payload(ROW, inst)
        assert g["online"] is False and g["battery"]["soc_pct"] is None and g["solar"] == []
        assert sv.validate({"version": 1, "gateways": [g]}) == []


FORECAST = {"timezone": "Australia/Sydney",
            "current": {"temp_c": 21.4, "condition": "Partly cloudy", "code": 2}}


class _Db:
    """app_config only: what the System view reads and writes."""

    def __init__(self):
        self.kv = {}


def _request(monkeypatch, *, forecast=FORECAST, body=None):
    rows = [{"id": "gw1", "name": "Garage", "enabled": 1, "host": "192.0.2.10"},
            {"id": "off", "name": "Off", "enabled": 0, "host": "192.0.2.11"},
            {"id": "default", "name": "aGate", "enabled": 1, "host": ""}]
    db = _Db()

    async def _rows(_db):
        return rows

    async def _get(_db, key, default=None):
        return _db.kv.get(key, default)

    async def _set(_db, key, value):
        _db.kv[key] = value

    monkeypatch.setattr(svapi, "get_gateways", _rows)
    monkeypatch.setattr(svapi, "get_app_config", _get)
    monkeypatch.setattr(svapi, "set_app_config", _set)

    async def _json():
        if isinstance(body, Exception):
            raise body
        return body

    def make(inst):
        fc = SimpleNamespace(cached=lambda: forecast)
        return SimpleNamespace(json=_json, app=SimpleNamespace(state=SimpleNamespace(
            db=db, solar_forecast=fc,
            registry=SimpleNamespace(get=lambda gid: inst if gid == "gw1" else None))))
    return db, make


async def test_route_lists_enabled_configured_gateways_with_scenes(monkeypatch):
    _, make = _request(monkeypatch)
    out = await svapi.get_system(make(await _inst(POINTS)))
    assert [g["id"] for g in out["gateways"]] == ["gw1"]
    assert set(out["gateways"][0]["scenes"]) == {"landscape", "portrait"}
    assert out["customise"]["endpoint"] == "api/system/customise"
    keys = {f["key"] for f in out["customise"]["fields"] if f["scope"] == "gw1"}
    assert keys >= {"name", "solar"}


async def test_site_name_and_weather_from_the_forecast_cache(monkeypatch):
    _, make = _request(monkeypatch)
    out = await svapi.get_system(make(await _inst(POINTS)))
    assert out["site"]["name"] == "Sydney" and out["site"]["weather"]["code"] == 2
    assert out["strip"]["name"] == "Sydney" and "Partly cloudy" in out["strip"]["weather"]["text"]


async def test_no_forecast_no_site_name_or_weather(monkeypatch):
    _, make = _request(monkeypatch, forecast=None)
    out = await svapi.get_system(make(await _inst(POINTS)))
    assert out["site"] == {} and out["strip"] == {"name": ""}


async def test_customise_saved_and_applied(monkeypatch):
    db, make = _request(monkeypatch, body={"site": {"name": "Home"},
                                           "gw1": {"name": "Shed", "legs": "", "solar": "apbox"}})
    inst = await _inst(POINTS)
    saved = await svapi.put_system_customise(make(inst))
    assert saved == {"ok": True, "saved": {"site": {"name": "Home"},
                                           "gw1": {"name": "Shed", "solar": "apbox"}}}
    out = await svapi.get_system(make(inst))
    assert out["site"]["name"] == "Home" and out["gateways"][0]["name"] == "Shed"
    assert {s["where"] for s in out["gateways"][0]["solar"]} == {"apbox"}
    # The form shows the reading beside the answer.
    name = next(f for f in out["customise"]["fields"] if f["scope"] == "gw1" and f["key"] == "name")
    assert name["value"] == "Shed" and name["auto"] == "Garage"


async def test_customise_rejects_unknown_settings_and_bad_json(monkeypatch):
    from fastapi import HTTPException
    for body in ({"gw1": {"colour": "red"}}, {"nope": {}}, ["x"], ValueError("bad json")):
        db, make = _request(monkeypatch, body=body)
        with pytest.raises(HTTPException) as e:
            await svapi.put_system_customise(make(await _inst(POINTS)))
        assert e.value.status_code == 400 and db.kv == {}


def test_page_mounts_the_overlay_and_offers_it_in_both_navs():
    index = (TPL / "index.html").read_text()
    assert 'FWHSystemView.mount({endpoint: "api/system"' in index
    assert 'href="system-view/system-view.css' in index
    assert 'src="system-view/system-view.js' in index
    for part in ("partials/sidebar.html", "partials/topbar.html", "partials/bottom_nav.html"):
        assert "FWHSystemView.open()" in (TPL / part).read_text(), part


def test_chips_follow_the_picker_and_the_logo_lines_up_with_the_top_bar():
    index = (TPL / "index.html").read_text()
    assert '"--topbar-h"' in index
    assert "onGateway:" in index and "FWHSystemView.showGateway(" in index
    assert "min-height: var(--topbar-h" in (TPL / "partials/sidebar.html").read_text()
