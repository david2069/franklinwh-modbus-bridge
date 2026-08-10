"""Unit tests for multi-HA entity access (client + registry + CRUD)."""

import asyncio
import contextlib
import json
import socket

import httpx
import pytest
import respx
from websockets.asyncio.server import serve

from franklinwh_bridge.gateway.ha import HaAuthError, HaInstance, HaRegistry, coerce_state
from franklinwh_bridge.store.db import (
    create_ha_instance,
    delete_ha_instance,
    get_ha_instance,
    get_ha_instances,
    init_db,
    update_ha_instance,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "ha.db")
    yield conn
    await conn.close()


# ── state coercion ────────────────────────────────────────────


def test_coerce_state():
    assert coerce_state("23.5") == 23.5
    assert coerce_state("42") == 42.0
    assert coerce_state("on") is True
    assert coerce_state("off") is False
    assert coerce_state("unavailable") is None
    assert coerce_state("unknown") is None
    assert coerce_state("") is None
    assert coerce_state(None) is None
    assert coerce_state("heat") == "heat"  # enum passthrough


# ── instance values + catalog (cache set directly) ────────────


def _inst(id_, name):
    inst = HaInstance({"id": id_, "name": name, "base_url": "http://h", "token": "t"})
    inst._states = {
        "sensor.amber_price": {
            "state": "31.2",
            "attributes": {"unit_of_measurement": "c/kWh", "friendly_name": "Amber Price"},
        },
        "binary_sensor.grid": {"state": "on", "attributes": {"friendly_name": "Grid"}},
        "sensor.dead": {"state": "unavailable", "attributes": {}},
    }
    return inst


def test_instance_values_namespaced_and_coerced():
    vals = _inst("pricing", "Pricing HA").values()
    assert vals["ha:pricing:sensor.amber_price"] == 31.2
    assert vals["ha:pricing:binary_sensor.grid"] is True
    assert vals["ha:pricing:sensor.dead"] is None


def test_instance_catalog_shape():
    cat = {c["id"]: c for c in _inst("pricing", "Pricing HA").catalog()}
    price = cat["ha:pricing:sensor.amber_price"]
    assert price["label"] == "Pricing HA: Amber Price"
    assert price["unit"] == "c/kWh"
    assert price["kind"] == "number"
    assert price["value"] == 31.2
    assert cat["ha:pricing:binary_sensor.grid"]["kind"] == "bool"


# ── registry merges instances ─────────────────────────────────


async def test_registry_merges_instances(db):
    reg = HaRegistry(db)
    reg._instances = {"a": _inst("a", "A"), "b": _inst("b", "B")}
    vals = reg.entity_values()
    assert vals["ha:a:sensor.amber_price"] == 31.2
    assert vals["ha:b:binary_sensor.grid"] is True
    assert len(reg.catalog()) == 6  # 3 entities × 2 instances


# ── live refresh via mocked HTTP ──────────────────────────────


@respx.mock
async def test_refresh_pulls_states():
    respx.get("http://ha.local/api/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "sensor.temp",
                    "state": "21.4",
                    "attributes": {"unit_of_measurement": "°C"},
                },
                {"entity_id": "sensor.na", "state": "unavailable", "attributes": {}},
            ],
        )
    )
    inst = HaInstance({"id": "home", "name": "Home", "base_url": "http://ha.local", "token": "abc"})
    await inst.refresh()
    assert inst.connected is True
    assert inst.values()["ha:home:sensor.temp"] == 21.4
    assert inst.values()["ha:home:sensor.na"] is None


@respx.mock
async def test_refresh_error_sets_disconnected():
    respx.get("http://ha.local/api/states").mock(return_value=httpx.Response(401))
    inst = HaInstance({"id": "home", "name": "Home", "base_url": "http://ha.local", "token": "bad"})
    await inst.refresh()
    assert inst.connected is False
    assert inst.last_error is not None


# ── CRUD + default uniqueness ─────────────────────────────────


async def test_ha_crud_and_single_default(db):
    a = await create_ha_instance(db, "Home", "http://ha1:8123/", token="t1", is_default=True)
    assert a["id"].startswith("ha_")
    assert a["base_url"] == "http://ha1:8123"  # trailing slash stripped
    assert a["is_default"] is True

    b = await create_ha_instance(db, "Pricing", "http://ha2:8123", is_default=True)
    # setting b default must clear a's default
    assert (await get_ha_instance(db, a["id"]))["is_default"] is False
    assert (await get_ha_instance(db, b["id"]))["is_default"] is True

    upd = await update_ha_instance(db, a["id"], enabled=False, name="Home renamed")
    assert upd["enabled"] is False and upd["name"] == "Home renamed"

    assert len(await get_ha_instances(db)) == 2
    assert await delete_ha_instance(db, a["id"]) is True
    assert await get_ha_instance(db, a["id"]) is None


# ── snapshot pass-through ─────────────────────────────────────


def test_snapshot_passes_through_ha_keys():
    from datetime import datetime

    from franklinwh_bridge.gateway.scheduler_sensors import snapshot

    snap = snapshot(
        {"soc": 55, "ha:pricing:sensor.amber_price": 31.2}, datetime(2026, 6, 17, 12, 0)
    )
    assert snap["battery.soc_pct"] == 55.0  # static sensor still works
    assert snap["ha:pricing:sensor.amber_price"] == 31.2  # HA value passed through


# ── WebSocket: URL derivation + pure cache ops ────────────────


def test_ws_url_derivation():
    assert HaInstance({"id": "a", "name": "A", "base_url": "http://h:8123"}).ws_url == (
        "ws://h:8123/api/websocket"
    )
    assert HaInstance({"id": "a", "name": "A", "base_url": "https://h:8123/"}).ws_url == (
        "wss://h:8123/api/websocket"
    )


def test_seed_and_apply_state_changed():
    inst = HaInstance({"id": "home", "name": "Home", "base_url": "http://h"})
    inst._seed_states(
        [
            {
                "entity_id": "sensor.temp",
                "state": "20.0",
                "attributes": {"unit_of_measurement": "C"},
            },
            {"not_an_entity": True},  # ignored
        ]
    )
    assert inst.values()["ha:home:sensor.temp"] == 20.0

    # update
    inst._apply_state_changed(
        {"entity_id": "sensor.temp", "new_state": {"state": "25.5", "attributes": {}}}
    )
    assert inst.values()["ha:home:sensor.temp"] == 25.5

    # removal (new_state None) drops the entity
    inst._apply_state_changed({"entity_id": "sensor.temp", "new_state": None})
    assert "ha:home:sensor.temp" not in inst.values()

    # missing entity_id is a no-op
    inst._apply_state_changed({"new_state": {"state": "1"}})


# ── WebSocket: live session against a real fake-HA server ─────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _fake_ha(ws, *, token="testtoken", push_event=True):
    """Minimal HA WS server: auth → get_states seed → subscribe ack → 1 event."""
    await ws.send(json.dumps({"type": "auth_required"}))
    auth = json.loads(await ws.recv())
    if auth.get("access_token") != token:
        await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))
        return
    await ws.send(json.dumps({"type": "auth_ok"}))
    # client sends get_states (id 1) then subscribe_events (id 2)
    await ws.recv()
    await ws.recv()
    await ws.send(
        json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.temp",
                        "state": "20.0",
                        "attributes": {"unit_of_measurement": "C"},
                    }
                ],
            }
        )
    )
    await ws.send(json.dumps({"id": 2, "type": "result", "success": True, "result": None}))
    if push_event:
        await ws.send(
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "data": {
                            "entity_id": "sensor.temp",
                            "new_state": {"state": "25.5", "attributes": {}},
                        },
                    },
                }
            )
        )
    await asyncio.sleep(5)  # hold the connection open


async def test_ws_live_seeds_then_applies_events():
    port = _free_port()
    async with serve(_fake_ha, "localhost", port):
        inst = HaInstance(
            {
                "id": "home",
                "name": "Home",
                "base_url": f"http://localhost:{port}",
                "token": "testtoken",
            }
        )
        task = asyncio.create_task(inst.run_live())
        try:
            for _ in range(50):  # up to ~5s
                if inst.transport == "ws" and inst.values().get("ha:home:sensor.temp") == 25.5:
                    break
                await asyncio.sleep(0.1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert inst.connected is True
    assert inst.transport == "ws"
    assert inst.values()["ha:home:sensor.temp"] == 25.5  # live event applied


async def test_ws_auth_invalid_raises():
    port = _free_port()

    async def bad(ws):
        await _fake_ha(ws, token="the-right-one")

    async with serve(bad, "localhost", port):
        inst = HaInstance(
            {"id": "home", "name": "Home", "base_url": f"http://localhost:{port}", "token": "wrong"}
        )
        with pytest.raises(HaAuthError):
            await inst._live_session()
