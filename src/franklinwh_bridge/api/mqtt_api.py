"""MQTT administration REST endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.config.environment import detect_environment
from franklinwh_bridge.store.db import get_mqtt_config, set_mqtt_config

router = APIRouter(prefix="/api/mqtt", tags=["mqtt"])


class MqttConfigUpdate(BaseModel):
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    tls_mode: str | None = Field(default=None, pattern="^(off|tls|tls_insecure)$")
    enabled: bool | None = None
    client_id: str | None = None
    qos: int | None = Field(default=None, ge=0, le=2)
    retain_discovery: bool | None = None
    topic_prefix: str | None = None
    discovery_prefix: str | None = None


def _redact_config(config: dict) -> dict:
    """Redact sensitive fields for API responses."""
    out = dict(config)
    if out.get("password"):
        out["password"] = "***"
    return out


@router.get("/config")
async def get_config(request: Request):
    db: aiosqlite.Connection = request.app.state.db
    config = await get_mqtt_config(db)
    return _redact_config(config)


@router.patch("/config")
async def update_config(body: MqttConfigUpdate, request: Request):
    db: aiosqlite.Connection = request.app.state.db
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    config = await set_mqtt_config(db, updates)
    return _redact_config(config)


@router.get("/status")
async def get_status(request: Request):
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    if publisher is None:
        return {"status": "not_configured", "connected": False}

    state = publisher.state
    return {
        "status": "started" if state.connected else "disconnected",
        "connected": state.connected,
        "messages_sent": state.messages_sent,
        "last_publish_ts": state.last_publish_ts,
        "last_error": state.last_error,
        "discovery_published": state.discovery_published,
        "entity_count": len(publisher.entities),
        # The broker actually in use. As an add-on it comes from the Supervisor
        # and is never written to the stored config, so /config alone showed
        # the "localhost" placeholder while connected to Mosquitto.
        "broker": {
            "host": getattr(publisher, "_host", None),
            "port": getattr(publisher, "_port", None),
            "username": getattr(publisher, "_username", None),
            "source": (getattr(request.app.state, "mqtt_broker", None) or {}).get("source"),
        },
    }


@router.post("/reconnect")
async def reconnect(request: Request):
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    if publisher is None:
        raise HTTPException(503, "MQTT publisher not configured")
    await publisher.stop()
    await publisher.start()
    return {"status": "reconnecting"}


@router.post("/publish")
async def republish_discovery(request: Request):
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    if publisher is None:
        raise HTTPException(503, "MQTT publisher not configured")
    publisher.request_discovery_republish()
    return {"status": "discovery_republish_queued"}


@router.post("/unpublish")
async def unpublish_discovery(request: Request):
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    if publisher is None:
        raise HTTPException(503, "MQTT publisher not configured")
    count = await publisher.unpublish_discovery()
    return {"status": "discovery_unpublished", "topics_cleared": count}


@router.get("/detect-broker")
async def detect_broker():
    env = detect_environment()
    if env == "ha_addon":
        return await _detect_mosquitto_addon()
    return {
        "detected": False,
        "source": "manual",
        "note": "Docker/Dev mode — configure your external MQTT broker",
    }


@router.post("/test")
async def test_connection(request: Request):
    """Test TCP connectivity.  Accepts optional {host, port} in the body
    to test *before* saving; falls back to the stored config."""
    db: aiosqlite.Connection = request.app.state.db
    body: dict = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    host = body.get("host") if body else None
    port = body.get("port") if body else None

    if not host or not port:
        config = await get_mqtt_config(db)
        host = host or config.get("host")
        port = port or config.get("port", 1883)

    if not host:
        raise HTTPException(400, "No MQTT host configured")

    try:
        result = await asyncio.wait_for(
            _test_tcp_connect(host, int(port)),
            timeout=5.0,
        )
        return {"ok": result.get("success", False), **result}
    except TimeoutError:
        return {"ok": False, "error": "Connection timed out (5s)"}


@router.get("/topics")
async def get_topics(request: Request):
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    if publisher is None:
        return {"topics": []}

    def _describe(entities, short_id) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entity in entities or []:
            entry: dict[str, Any] = {
                "slug": entity.slug,
                "name": entity.name,
                "ha_type": entity.ha_type,
                "source": entity.source,
                "stat_key": entity.stat_key,
                "value_scale": entity.value_scale,
                "value_precision": entity.value_precision,
                "state_topic": entity.state_topic(short_id),
                "discovery_topic": entity.discovery_topic(short_id),
            }
            cmd = entity.command_topic(short_id)
            if cmd:
                entry["command_topic"] = cmd
            out.append(entry)
        return out

    # Every gateway that publishes, not just the default one.
    #
    # This endpoint used to read publisher.device_info / .entities only — the
    # legacy single-device path — so a second gateway registered through
    # register_device() was structurally invisible here and in the Settings UI.
    # A mock publishing 61 entities showed as nothing at all, which made it
    # impossible to tell "not publishing" from "not displayed".
    gateways: list[dict[str, Any]] = []

    default_info = publisher.device_info
    if default_info is not None:
        gateways.append({
            "gateway_id": publisher.gateway_id,
            "short_id": default_info.short_id,
            "name": getattr(default_info, "name", None) or publisher.gateway_id,
            "is_default": True,
            "topics": _describe(publisher.entities, default_info.short_id),
        })

    for gw_id in publisher.devices():
        dev = publisher.get_device(gw_id)
        if dev is None or dev.device_info is None:
            continue
        gateways.append({
            "gateway_id": gw_id,
            "short_id": dev.device_info.short_id,
            "name": getattr(dev.device_info, "name", None) or gw_id,
            "is_default": False,
            "topics": _describe(dev.entities, dev.device_info.short_id),
        })

    for g in gateways:
        g["entity_count"] = len(g["topics"])

    if not gateways:
        return {"topics": [], "gateways": [], "note": "No device info set yet"}

    # Back-compat: `topics`/`short_id` keep describing ONE gateway — the one
    # asked for, else the default — so existing callers are unaffected while
    # `gateways` carries the full picture.
    wanted = request.query_params.get("gateway")
    sel = next((g for g in gateways if g["gateway_id"] == wanted), None) or gateways[0]

    return {
        "short_id": sel["short_id"],
        "gateway_id": sel["gateway_id"],
        "entity_count": sel["entity_count"],
        "topics": sel["topics"],
        "gateways": [
            {k: v for k, v in g.items() if k != "topics"} | {"topics": g["topics"]}
            for g in gateways
        ],
        "total_entity_count": sum(g["entity_count"] for g in gateways),
    }


async def _detect_mosquitto_addon() -> dict:
    """Check if the HA Mosquitto add-on is reachable."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo("core-mosquitto", 1883, type=socket.SOCK_STREAM),
        )
        return {
            "detected": True,
            "host": "core-mosquitto",
            "port": 1883,
            "source": "ha_addon_auto",
            "note": "Mosquitto Add-on detected",
        }
    except (socket.gaierror, OSError):
        return {
            "detected": False,
            "source": "ha_addon_auto",
            "error": "Mosquitto Add-on not found — install it from the HA Add-on Store",
        }


async def _test_tcp_connect(host: str, port: int) -> dict:
    """Test raw TCP connectivity to the broker."""
    try:
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        await loop.run_in_executor(None, sock.connect, (host, port))
        sock.close()
        return {"success": True, "host": host, "port": port}
    except (ConnectionRefusedError, OSError) as exc:
        return {"success": False, "host": host, "port": port, "error": str(exc)}
