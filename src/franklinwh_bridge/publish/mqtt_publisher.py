"""Queue-based MQTT publisher with HA Discovery and availability.

Uses the EntityDef registry (entities.py) for curated HA entity definitions
with per-entity state topics, matching the HA integrator's topic layout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass

import aiomqtt

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES, EntityDef

logger = logging.getLogger(__name__)

TOPIC_PREFIX = "franklinwh"


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
class DeviceInfo:
    serial: str
    manufacturer: str = "FranklinWH Technologies Co., Ltd"
    model: str = "aGate"
    firmware: str = ""
    name: str = ""

    @property
    def short_id(self) -> str:
        return self.serial[-8:] if len(self.serial) >= 8 else self.serial

    def ha_device_block(self, app_version: str = "") -> dict:
        name = self.name or f"FranklinWH {self.short_id}"
        sw = self.firmware
        if app_version:
            sw = f"{self.firmware} (bridge: v{app_version})" if sw else f"bridge: v{app_version}"
        block: dict = {
            "identifiers": [f"franklinwh_{self.serial}"],
            "name": name,
            "model": self.model,
            "manufacturer": self.manufacturer,
        }
        if self.serial:
            block["serial_number"] = self.serial
        if sw:
            block["sw_version"] = sw
        return block


def build_discovery_payload(
    entity: EntityDef,
    device_info: DeviceInfo,
    app_version: str = "",
) -> dict:
    """Build an HA MQTT Discovery config payload for a single entity."""
    short_id = device_info.short_id
    avail_topic = f"{TOPIC_PREFIX}/{short_id}/availability"

    payload: dict = {
        "unique_id": entity.unique_id(short_id),
        "name": entity.name,
        "state_topic": entity.state_topic(short_id),
        "availability_topic": avail_topic,
        "device": device_info.ha_device_block(app_version),
    }

    if entity.unit:
        payload["unit_of_measurement"] = entity.unit
    if entity.device_class:
        payload["device_class"] = entity.device_class
    if entity.state_class:
        payload["state_class"] = entity.state_class
    if entity.icon:
        payload["icon"] = entity.icon
    if entity.entity_category:
        payload["entity_category"] = entity.entity_category

    cmd_topic = entity.command_topic(short_id)
    if cmd_topic:
        payload["command_topic"] = cmd_topic

    if entity.ha_type == "select" and entity.options:
        payload["options"] = entity.options
    if entity.ha_type == "number":
        if entity.min_val is not None:
            payload["min"] = entity.min_val
        if entity.max_val is not None:
            payload["max"] = entity.max_val
        if entity.step is not None:
            payload["step"] = entity.step

    return payload


class MqttPublisher:
    """Async MQTT publisher with queue, HA Discovery, and reconnect."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        gateway_id: str = "default",
        client_id: str = "franklinwh_bridge",
        qos: int = 0,
        topic_prefix: str = "franklinwh",
        discovery_prefix: str = "homeassistant",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._gateway_id = gateway_id
        self._client_id = client_id
        self._default_qos = qos
        self._topic_prefix = topic_prefix
        self._discovery_prefix = discovery_prefix
        self._queue: asyncio.Queue[MqttMessage] = asyncio.Queue(maxsize=1000)
        self._state = MqttState()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._device_info: DeviceInfo | None = None
        self._entities: list[EntityDef] = list(BRIDGE_ENTITIES)
        self._command_handler: CommandHandler | None = None

    @property
    def state(self) -> MqttState:
        return self._state

    @property
    def entities(self) -> list[EntityDef]:
        return self._entities

    @property
    def device_info(self) -> DeviceInfo | None:
        return self._device_info

    def set_device_info(self, info: DeviceInfo) -> None:
        self._device_info = info
        self._state.discovery_published = False

    def set_command_handler(self, handler: CommandHandler) -> None:
        self._command_handler = handler

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        """Publish HA Discovery config for all registered entities."""
        if not self._device_info:
            return

        from franklinwh_bridge import __version__

        short_id = self._device_info.short_id
        for entity in self._entities:
            topic = entity.discovery_topic(short_id)
            payload = build_discovery_payload(
                entity, self._device_info, app_version=__version__
            )
            await client.publish(topic, json.dumps(payload), retain=True)

        logger.info("Published HA Discovery for %d entities", len(self._entities))
        self._state.discovery_published = True

    async def _publish_availability(self, client: aiomqtt.Client, online: bool) -> None:
        if not self._device_info:
            return
        topic = f"{TOPIC_PREFIX}/{self._device_info.short_id}/availability"
        await client.publish(topic, "online" if online else "offline", retain=True)

    async def queue_sample(self, sample: Sample) -> None:
        """Queue per-entity state messages from a poller sample."""
        if not self._device_info:
            return

        points = dict(sample.points)
        if self._command_handler:
            points.update(self._command_handler.virtual_points)

        short_id = self._device_info.short_id
        for entity in self._entities:
            if not entity.stat_key:
                continue
            value = points.get(entity.stat_key)
            if value is None:
                continue

            topic = entity.state_topic(short_id)
            msg = MqttMessage(topic=topic, payload=entity.format_value(value), retain=True)
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("MQTT queue full, dropping message for %s", entity.slug)
                break

    def _backoff_delay(self, attempt: int) -> float:
        return min(5.0 * (2 ** attempt), 60.0)

    async def _handle_mqtt_message(self, message: aiomqtt.Message) -> None:
        """Dispatch an incoming MQTT command message."""
        if not self._command_handler:
            return
        topic_str = str(message.topic)
        parts = topic_str.split("/")
        if len(parts) >= 5 and parts[2] == "control" and parts[-1] == "set":
            slug = parts[3]
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
            await self._command_handler.handle_command(slug, payload)

    async def _subscribe_listener(self, client: aiomqtt.Client) -> None:
        """Background task: listen for incoming command messages."""
        async for message in client.messages:
            await self._handle_mqtt_message(message)

    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    identifier=self._client_id,
                ) as client:
                    self._state.connected = True
                    self._state.last_error = None
                    attempt = 0
                    logger.info("MQTT connected to %s:%d", self._host, self._port)

                    await self._publish_availability(client, online=True)
                    if self._device_info and not self._state.discovery_published:
                        await self._publish_discovery(client)

                    listener_task = None
                    if self._device_info and self._command_handler:
                        cmd_topic = f"{TOPIC_PREFIX}/{self._device_info.short_id}/control/+/set"
                        await client.subscribe(cmd_topic)
                        listener_task = asyncio.create_task(self._subscribe_listener(client))
                        logger.info("Subscribed to command topics: %s", cmd_topic)

                    try:
                        while not self._stop_event.is_set():
                            try:
                                msg = await asyncio.wait_for(
                                    self._queue.get(), timeout=1.0
                                )
                                await client.publish(
                                    msg.topic, msg.payload, retain=msg.retain, qos=msg.qos
                                )
                                self._state.messages_sent += 1
                                self._state.last_publish_ts = time.time()
                            except TimeoutError:
                                pass

                            if self._device_info and not self._state.discovery_published:
                                await self._publish_discovery(client)
                                await self._publish_availability(client, online=True)
                    finally:
                        if listener_task:
                            listener_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await listener_task

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

    @classmethod
    def from_db_config(cls, config: dict, gateway_id: str = "default") -> MqttPublisher:
        return cls(
            host=config.get("host", "localhost"),
            port=config.get("port", 1883),
            username=config.get("username"),
            password=config.get("password"),
            gateway_id=gateway_id,
            client_id=config.get("client_id", "franklinwh_bridge"),
            qos=config.get("qos", 0),
            topic_prefix=config.get("topic_prefix", "franklinwh"),
            discovery_prefix=config.get("discovery_prefix", "homeassistant"),
        )

    async def reconfigure(self, config: dict) -> None:
        """Apply new config by restarting the connection loop."""
        was_running = self._task and not self._task.done()
        if was_running:
            await self.stop()

        self._host = config.get("host", self._host)
        self._port = config.get("port", self._port)
        self._username = config.get("username", self._username)
        self._password = config.get("password", self._password)
        self._client_id = config.get("client_id", self._client_id)
        self._default_qos = config.get("qos", self._default_qos)
        self._topic_prefix = config.get("topic_prefix", self._topic_prefix)
        self._discovery_prefix = config.get("discovery_prefix", self._discovery_prefix)
        self._state.discovery_published = False

        if was_running:
            await self.start()

    def request_discovery_republish(self) -> None:
        """Flag discovery for re-publish on the next connection cycle."""
        self._state.discovery_published = False

    async def unpublish_discovery(self) -> int:
        """Send empty retained payloads to all discovery topics (tombstones)."""
        if not self._device_info:
            return 0

        short_id = self._device_info.short_id
        count = 0
        try:
            async with aiomqtt.Client(
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            ) as client:
                for entity in self._entities:
                    topic = entity.discovery_topic(short_id)
                    await client.publish(topic, b"", retain=True)
                    count += 1
                avail_topic = f"{self._topic_prefix}/{short_id}/availability"
                await client.publish(avail_topic, b"", retain=True)
        except Exception as exc:
            logger.error("Failed to unpublish discovery: %s", exc)
            raise

        logger.info("Unpublished %d discovery topics", count)
        self._state.discovery_published = False
        return count
