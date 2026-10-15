"""HA dashboard card YAML from the registries (#86).

No network: HaInstance.ws_call is replaced with a fake that serves canned
device/entity registry lists.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api.ha_api import router as ha_router
from franklinwh_bridge.gateway.ha import HaInstance, HaRegistry
from franklinwh_bridge.publish.dashboard_yaml import (
    DashboardError,
    build_dashboard,
    our_devices,
    render,
    select_ours,
)
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.db import create_ha_instance, init_db

SERIAL = "10060006A02F00000001"
SID = SERIAL[-8:]  # default gateway short_id


def _publisher(prefix: str = "franklinwh") -> MqttPublisher:
    pub = MqttPublisher(topic_prefix=prefix)
    pub.set_device_info(DeviceInfo(serial=SERIAL, name="aGate Home"))
    return pub


def _ent(eid: str, uid: str, device: str = "dev_ours", **extra) -> dict:
    return {
        "entity_id": eid,
        "unique_id": uid,
        "platform": "mqtt",
        "device_id": device,
        "disabled_by": None,
        **extra,
    }


def _registries(prefix: str = "franklinwh") -> tuple[list[dict], list[dict]]:
    devices = [
        {
            "id": "dev_ours",
            "name": "aGate Home",
            "name_by_user": None,
            "identifiers": [["mqtt", f"{prefix}_{SERIAL}"]],
            "sw_version": "Modbus Bridge v1.0.0",
        },
        # Another producer (FWHAI / Local Bridge) for the same aGate, with the
        # SAME franklinwh_<sid>_<slug> unique_ids but its own device.
        {
            "id": "dev_foreign",
            "name": "FranklinWH (FWHAI)",
            "identifiers": [["mqtt", "fwhai_" + SERIAL]],
            "sw_version": "fwhai 2.0",
        },
    ]
    p = f"{prefix}_{SID}_"
    entities = [
        _ent("sensor.state_of_charge", p + "battery_soc"),
        _ent("sensor.grid_power", p + "grid_power_kw"),
        # User-renamed, and HA's _2 suffix: used verbatim.
        _ent("sensor.my_battery_power", p + "battery_power_kw"),
        _ent("select.operating_mode_2", p + "operating_mode"),
        _ent("sensor.grid_voltage", p + "grid_voltage_v"),
        # operating_mode_sensor shares a prefix with operating_mode; exact slug.
        _ent("sensor.operating_mode", p + "operating_mode_sensor"),
        _ent("sensor.inverter_state", p + "inverter_state"),
        _ent("sensor.unknown_thing", p + "some_future_slug"),
        _ent("sensor.solar_power", p + "solar_power_kw"),
        _ent("sensor.disabled_one", p + "battery_soh", disabled_by="user"),
        # Foreign rows: identical unique_ids, different device.
        _ent("sensor.fwhai_soc", p + "battery_soc", device="dev_foreign"),
        _ent("sensor.fwhai_grid", p + "grid_power_kw", device="dev_foreign"),
        # Not mqtt.
        {**_ent("sensor.template_soc", p + "battery_soc"), "platform": "template"},
    ]
    return devices, entities


class _FakeInst:
    def __init__(self, devices, entities, fail: Exception | None = None):
        self.id, self.name = "ha_1", "Home HA"
        self._data = {
            "config/device_registry/list": devices,
            "config/entity_registry/list": entities,
        }
        self._fail = fail
        self.calls: list[str] = []

    async def ws_call(self, command: dict):
        self.calls.append(command["type"])
        if self._fail:
            raise self._fail
        return self._data[command["type"]]


def _ids(text: str) -> list[str]:
    doc = yaml.safe_load(text)
    cards = doc["cards"] if doc["type"] == "vertical-stack" else [doc]
    return [row["entity"] for c in cards for row in c["entities"]]


async def test_grouped_layout_order_and_foreign_excluded():
    out = await build_dashboard(_FakeInst(*_registries()), _publisher())
    doc = yaml.safe_load(out["yaml"])
    assert doc["type"] == "vertical-stack"
    titles = [c["title"] for c in doc["cards"]]
    assert titles == [
        "Modbus Bridge — Power & Energy",
        "Modbus Bridge — Battery",
        "Modbus Bridge — Controls",
        "Modbus Bridge — Status",
    ]
    for c in doc["cards"]:
        assert c["type"] == "entities"
        assert c["show_header_toggle"] is False
        assert c["state_color"] is True
    by_title = {c["title"]: [r["entity"] for r in c["entities"]] for c in doc["cards"]}
    # BRIDGE_ENTITIES order within a group: grid_power_kw precedes solar_power_kw.
    assert by_title["Modbus Bridge — Power & Energy"] == ["sensor.grid_power", "sensor.solar_power"]
    # battery_soc precedes battery_power_kw; renamed id used verbatim.
    assert by_title["Modbus Bridge — Battery"] == [
        "sensor.state_of_charge",
        "sensor.my_battery_power",
    ]
    assert by_title["Modbus Bridge — Controls"] == ["select.operating_mode_2"]
    assert by_title["Modbus Bridge — Status"] == ["sensor.grid_voltage"]
    ids = _ids(out["yaml"])
    assert not {"sensor.fwhai_soc", "sensor.fwhai_grid", "sensor.template_soc"} & set(ids)
    assert "sensor.disabled_one" not in ids
    assert out["entity_count"] == 6
    assert out["devices"][0]["ha_device_id"] == "dev_ours"
    assert "theme" not in out["yaml"]
    assert "custom:" not in out["yaml"]


async def test_diagnostics_toggle_includes_diag_and_unknown_slugs():
    out = await build_dashboard(_FakeInst(*_registries()), _publisher(), include_diagnostics=True)
    doc = yaml.safe_load(out["yaml"])
    last = doc["cards"][-1]
    assert last["title"] == "Modbus Bridge — Diagnostics"
    diag = [r["entity"] for r in last["entities"]]
    # Known diagnostics in BRIDGE_ENTITIES order, unknown slug after them.
    assert diag == ["sensor.inverter_state", "sensor.operating_mode", "sensor.unknown_thing"]


async def test_single_layout():
    out = await build_dashboard(_FakeInst(*_registries()), _publisher(), layout="single")
    doc = yaml.safe_load(out["yaml"])
    assert doc["type"] == "entities"
    assert doc["title"] == "Modbus Bridge"
    assert doc["show_header_toggle"] is False and doc["state_color"] is True
    assert [r["entity"] for r in doc["entities"]] == [
        "sensor.grid_power",
        "sensor.solar_power",
        "sensor.state_of_charge",
        "sensor.my_battery_power",
        "select.operating_mode_2",
        "sensor.grid_voltage",
    ]


async def test_output_is_deterministic_and_matches_fwhai_shape():
    a = await build_dashboard(_FakeInst(*_registries()), _publisher())
    b = await build_dashboard(_FakeInst(*_registries()), _publisher())
    assert a["yaml"] == b["yaml"]
    assert a["yaml"].startswith(
        "type: vertical-stack\ncards:\n"
        "  - type: entities\n"
        "    title: Modbus Bridge — Power & Energy\n"
        "    show_header_toggle: false\n"
        "    state_color: true\n"
        "    entities:\n"
        "      - entity: sensor.grid_power\n"
    )


async def test_custom_namespace():
    pub = _publisher("fwh_mb")
    devices, entities = _registries("fwh_mb")
    # Legacy-prefix rows on our device (left over from before a namespace
    # change) are not ours under the current prefix.
    entities.append(_ent("sensor.old_soc", f"franklinwh_{SID}_battery_soc"))
    out = await build_dashboard(_FakeInst(devices, entities), pub)
    ids = _ids(out["yaml"])
    assert "sensor.state_of_charge" in ids
    assert "sensor.old_soc" not in ids
    assert out["devices"][0]["identifier"] == f"fwh_mb_{SERIAL}"

    # The default-prefix publisher doesn't recognise an fwh_mb device at all.
    with pytest.raises(DashboardError) as err:
        await build_dashboard(_FakeInst(devices, entities), _publisher("franklinwh"))
    assert err.value.status == 409


async def test_registry_failure_raises_and_no_yaml():
    inst = _FakeInst([], [], fail=RuntimeError("Unauthorized"))
    with pytest.raises(DashboardError) as err:
        await build_dashboard(inst, _publisher())
    assert err.value.status == 503
    assert "Unauthorized" in err.value.detail


async def test_no_entities_of_ours_is_an_error():
    devices, _ = _registries()
    with pytest.raises(DashboardError) as err:
        await build_dashboard(_FakeInst(devices, []), _publisher())
    assert err.value.status == 409


async def test_multi_device_titles_name_the_device():
    pub = _publisher()
    pub.register_device("Shed GW", DeviceInfo(serial="SHED0001", gateway_id="Shed GW"))
    shed_ident = pub.get_device("Shed GW").device_info.ha_device_block(prefix="franklinwh")[
        "identifiers"
    ][0]
    devices, entities = _registries()
    devices.append(
        {
            "id": "dev_shed",
            "name": "Shed",
            "name_by_user": "Shed: aGate",
            "identifiers": [["mqtt", shed_ident]],
        }
    )
    entities.append(_ent("sensor.shed_soc", "franklinwh_shed_gw_battery_soc", device="dev_shed"))
    ours = our_devices(pub)
    assert [d.identifier for d in ours] == [f"franklinwh_{SERIAL}", shed_ident]
    cards = select_ours(devices, entities, ours, "franklinwh")
    text = render(cards)
    doc = yaml.safe_load(text)  # "Shed: aGate" needs quoting to stay valid YAML
    titles = [c["title"] for c in doc["cards"]]
    assert "Modbus Bridge — aGate Home — Battery" in titles
    assert titles[-1] == "Modbus Bridge — Shed: aGate — Battery"


# ── route ─────────────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    db = await init_db(tmp_path / "dash.db")
    row = await create_ha_instance(
        db, name="Home HA", base_url="http://ha:8123", token="t", is_default=True
    )
    app = FastAPI()
    app.include_router(ha_router)
    app.state.db = db
    reg = HaRegistry(db)
    await reg.load()
    inst = reg.get(row["id"])
    inst.connected = True
    app.state.ha_registry = reg
    app.state.mqtt_publisher = _publisher()
    devices, entities = _registries()
    fake = _FakeInst(devices, entities)
    monkeypatch.setattr(HaInstance, "ws_call", lambda self, cmd: fake.ws_call(cmd))
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.app, ac.inst_id, ac.fake = app, row["id"], fake
        yield ac
    await db.close()


async def test_route_returns_yaml(client):
    r = await client.get("/api/ha/dashboard-yaml")
    assert r.status_code == 200
    body = r.json()
    assert body["layout"] == "grouped"
    assert body["instance"] == {"id": client.inst_id, "name": "Home HA"}
    assert body["entity_count"] == 6
    assert body["yaml"].startswith("type: vertical-stack")

    r = await client.get(
        f"/api/ha/dashboard-yaml?instance={client.inst_id}&layout=single&diagnostics=true"
    )
    assert r.status_code == 200
    assert r.json()["entity_count"] == 9
    assert r.json()["yaml"].startswith("type: entities")


async def test_route_errors(client):
    assert (await client.get("/api/ha/dashboard-yaml?layout=bogus")).status_code == 422
    assert (await client.get("/api/ha/dashboard-yaml?instance=nope")).status_code == 404

    client.fake._fail = RuntimeError("boom")
    r = await client.get("/api/ha/dashboard-yaml")
    assert r.status_code == 503 and "yaml" not in r.json()

    client.app.state.ha_registry.get(client.inst_id).connected = False
    r = await client.get("/api/ha/dashboard-yaml")
    assert r.status_code == 409 and "connected" in r.json()["detail"]

    client.app.state.mqtt_publisher = None
    assert (await client.get("/api/ha/dashboard-yaml")).status_code == 503


def test_route_is_behind_auth_like_the_rest_of_the_ha_router():
    """Mounted with the HA router's require_module + require_auth deps."""
    from franklinwh_bridge.api.auth import require_auth
    from franklinwh_bridge.main import app

    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/ha/dashboard-yaml")
    calls = {d.call for d in route.dependant.dependencies}
    assert require_auth in calls
    sibling = next(r for r in app.routes if getattr(r, "path", "") == "/api/ha/entities")
    assert {d.call for d in sibling.dependant.dependencies} == calls
