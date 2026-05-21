"""Queue-based MQTT publisher with HA Discovery and availability."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass

import aiomqtt

from franklinwh_bridge.modbus.sample import Sample

logger = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
TOPIC_PREFIX = "franklinwh"

HA_COMPONENT_MAP = {
    "W": ("sensor", "power", "W"),
    "Wh": ("sensor", "energy", "Wh"),
    "V": ("sensor", "voltage", "V"),
    "A": ("sensor", "current", "A"),
    "Hz": ("sensor", "frequency", "Hz"),
    "%": ("sensor", "battery", "%"),
}


@dataclass
class MqttMessage:
    topic: str
    payload: str
    retain: bool = False
    qos: int = 0


@dataclass
class MqttState:
    connected: bool = False
    last_publish_ts: float | None = None
    last_error: str | None = None
    messages_sent: int = 0
    discovery_published: bool = False


@dataclass
class EntityMapping:
    point_name: str
    ha_component: str
    device_class: str | None
    unit: str | None
    unique_id: str
    name: str


def build_entity_mappings(
    catalog: list[dict], gateway_id: str
) -> list[EntityMapping]:
    """Build HA entity mappings from the SunSpec catalog."""
    mappings: list[EntityMapping] = []
    for rec in catalog:
        point_name = rec["point_name"]
        unit = rec.get("unit")
        ha_component = "sensor"
        device_class = None

        if unit and unit in HA_COMPONENT_MAP:
            ha_component, device_class, _ = HA_COMPONENT_MAP[unit]

        unique_id = f"franklinwh_{gateway_id}_{point_name}".lower()
        display_name = point_name.replace("_", " ").title()

        mappings.append(EntityMapping(
            point_name=point_name,
            ha_component=ha_component,
            device_class=device_class,
            unit=unit,
            unique_id=unique_id,
            name=f"FranklinWH {display_name}",
        ))

    return mappings


def build_discovery_payload(
    mapping: EntityMapping, gateway_id: str
) -> dict:
    """Build an HA MQTT Discovery config payload for a single entity."""
    state_topic = f"{TOPIC_PREFIX}/{gateway_id}/state"
    avail_topic = f"{TOPIC_PREFIX}/{gateway_id}/availability"

    payload: dict = {
        "name": mapping.name,
        "unique_id": mapping.unique_id,
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{mapping.point_name} }}}}",
        "availability_topic": avail_topic,
        "device": {
            "identifiers": [f"franklinwh_{gateway_id}"],
            "name": f"FranklinWH {gateway_id}",
            "manufacturer": "FranklinWH",
            "model": "aGate",
        },
    }

    if mapping.device_class:
        payload["device_class"] = mapping.device_class
    if mapping.unit:
        payload["unit_of_measurement"] = mapping.unit
    if mapping.device_class:
        payload["state_class"] = "measurement"

    return payload


def build_discovery_topic(mapping: EntityMapping) -> str:
    return f"{DISCOVERY_PREFIX}/{mapping.ha_component}/{mapping.unique_id}/config"


class MqttPublisher:
    """Async MQTT publisher with queue, HA Discovery, and reconnect."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        gateway_id: str = "default",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._gateway_id = gateway_id
        self._queue: asyncio.Queue[MqttMessage] = asyncio.Queue(maxsize=1000)
        self._state = MqttState()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._entity_mappings: list[EntityMapping] = []

    @property
    def state(self) -> MqttState:
        return self._state

    @property
    def entity_mappings(self) -> list[EntityMapping]:
        return self._entity_mappings

    def set_entity_mappings(self, mappings: list[EntityMapping]) -> None:
        self._entity_mappings = mappings
        self._state.discovery_published = False

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        """Publish HA Discovery config for all mapped entities."""
        for mapping in self._entity_mappings:
            topic = build_discovery_topic(mapping)
            payload = build_discovery_payload(mapping, self._gateway_id)
            await client.publish(topic, json.dumps(payload), retain=True)

        logger.info("Published HA Discovery for %d entities", len(self._entity_mappings))
        self._state.discovery_published = True

    async def _publish_availability(self, client: aiomqtt.Client, online: bool) -> None:
        topic = f"{TOPIC_PREFIX}/{self._gateway_id}/availability"
        await client.publish(topic, "online" if online else "offline", retain=True)

    async def queue_sample(self, sample: Sample) -> None:
        """Queue a sample for MQTT publishing."""
        topic = f"{TOPIC_PREFIX}/{self._gateway_id}/state"
        payload = json.dumps(sample.points, default=str)
        msg = MqttMessage(topic=topic, payload=payload, retain=True)
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("MQTT queue full, dropping message")

    def _backoff_delay(self, attempt: int) -> float:
        return min(5.0 * (2 ** attempt), 60.0)

    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                ) as client:
                    self._state.connected = True
                    self._state.last_error = None
                    attempt = 0
                    logger.info("MQTT connected to %s:%d", self._host, self._port)

                    await self._publish_availability(client, online=True)
                    if self._entity_mappings and not self._state.discovery_published:
                        await self._publish_discovery(client)

                    while not self._stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(
                                self._queue.get(), timeout=1.0
                            )
                            await client.publish(
                                msg.topic, msg.payload, retain=msg.retain, qos=msg.qos
                            )
                            self._state.messages_sent += 1
                            self._state.last_publish_ts = __import__("time").time()
                        except TimeoutError:
                            continue

            except aiomqtt.MqttError as exc:
                self._state.connected = False
                self._state.last_error = str(exc)
                logger.error("MQTT error: %s", exc)
            except Exception as exc:
                self._state.connected = False
                self._state.last_error = str(exc)
                logger.error("MQTT unexpected error: %s", exc)

            if not self._stop_event.is_set():
                delay = self._backoff_delay(attempt)
                attempt += 1
                logger.info("MQTT reconnect in %.0fs", delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("MQTT publisher started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._state.connected = False
        logger.info("MQTT publisher stopped")
