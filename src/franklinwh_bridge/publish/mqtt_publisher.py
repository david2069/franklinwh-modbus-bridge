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
from typing import TYPE_CHECKING

import aiomqtt

if TYPE_CHECKING:
    import aiosqlite

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES, EntityDef

logger = logging.getLogger(__name__)

TOPIC_PREFIX = "franklinwh"


@dataclass
class GatewayDevice:
    """Per-gateway MQTT device state."""

    gateway_id: str
    device_info: DeviceInfo | None = None
    command_handler: CommandHandler | None = None
    ac_type: int = 0
    battery_port_count: int = 1
    disabled_slugs: set[str] | None = None
    entities: list[EntityDef] | None = None
    removed_entities: list[EntityDef] | None = None
    discovery_published: bool = False
    max_val_overrides: dict[str, float] | None = None

    def rebuild_entities(self) -> None:
        """Rebuild entity lists from phase + battery port + group filters."""
        disabled = self.disabled_slugs or set()
        ac = self.ac_type
        nport = self.battery_port_count
        self.entities = [
            e for e in BRIDGE_ENTITIES
            if (e.phase is None or e.phase <= ac + 1)
            and (e.battery_port is None or e.battery_port <= nport)
            and e.slug not in disabled
        ]
        active = {e.slug for e in self.entities}
        self.removed_entities = [
            e for e in BRIDGE_ENTITIES if e.slug not in active
        ]
        self.discovery_published = False


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
    gateway_id: str = "default"

    @property
    def _serial_tail(self) -> str:
        return self.serial[-8:] if len(self.serial) >= 8 else self.serial

    @property
    def short_id(self) -> str:
        """Topic/unique_id namespace for this device.

        The default gateway keeps the historic serial-only id so existing
        Home Assistant entities are preserved.  Additional gateways are
        prefixed with their gateway_id so two gateways that happen to report
        the same serial (e.g. both pointed at one physical aGate) get distinct
        MQTT topics and unique_ids instead of colliding in HA Discovery.
        """
        if self.gateway_id and self.gateway_id != "default":
            return f"{self.gateway_id}_{self._serial_tail}"
        return self._serial_tail

    def ha_device_block(self, app_version: str = "") -> dict:
        name = self.name or f"FranklinWH {self.short_id}"
        sw = self.firmware
        if app_version:
            sw = f"{self.firmware} (bridge: v{app_version})" if sw else f"bridge: v{app_version}"
        # Device identifier mirrors short_id's namespacing so duplicate serials
        # don't merge into one HA device.
        if self.gateway_id and self.gateway_id != "default":
            identifier = f"franklinwh_{self.gateway_id}_{self.serial}"
        else:
            identifier = f"franklinwh_{self.serial}"
        block: dict = {
            "identifiers": [identifier],
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

    if entity.ha_type == "binary_sensor":
        payload["payload_on"] = "1"
        payload["payload_off"] = "0"
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
        self._removed_entities: list[EntityDef] = []
        self._promoted_entities: list[EntityDef] = []  # catalog points promoted via groups
        self._ac_type: int = 0
        self._disabled_slugs: set[str] = set()
        self._command_handler: CommandHandler | None = None
        self._battery_port_count: int = 1
        # Per-slug overrides for discovery max_val (set by power limit detection)
        self._max_val_overrides: dict[str, float] = {}
        # Multi-gateway device registry
        self._devices: dict[str, GatewayDevice] = {}

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

    def _rebuild_entity_lists(self) -> None:
        """Rebuild active/removed entity lists from phase + group + battery filters.

        Call after any change to ``_ac_type``, ``_disabled_slugs``, or
        ``_battery_port_count``.
        """
        ac_type = self._ac_type
        disabled = self._disabled_slugs
        nport = self._battery_port_count

        new_entities = [
            e for e in BRIDGE_ENTITIES
            if (e.phase is None or e.phase <= ac_type + 1)
            and (e.battery_port is None or e.battery_port <= nport)
            and e.slug not in disabled
        ]
        # Promoted catalog points (already filtered to enabled groups).
        new_entities += self._promoted_entities
        active_slugs = {e.slug for e in new_entities}
        self._removed_entities = [
            e for e in BRIDGE_ENTITIES if e.slug not in active_slugs
        ]
        self._entities = new_entities
        self._state.discovery_published = False

    def set_ac_type(self, ac_type: int) -> None:
        """Configure phase filtering based on detected AC wiring type.

        ac_type 0 (single): publish phase=None + phase=1
        ac_type 1 (split):  publish phase=None + phase=1 + phase=2
        ac_type 2 (three):  publish all

        Entities removed by phase filtering are tracked so their stale
        retained MQTT discovery configs can be tombstoned on next publish.
        """
        self._ac_type = ac_type
        self._rebuild_entity_lists()
        AC_TYPE_NAMES = {0: "Single Phase", 1: "Split Phase", 2: "Three Phase"}
        phase_count = sum(1 for e in self._entities if e.phase is not None)
        logger.info(
            "AC type %s: publishing %d entities (%d per-phase, %d removed)",
            AC_TYPE_NAMES.get(ac_type, f"Unknown({ac_type})"),
            len(self._entities),
            phase_count,
            len(self._removed_entities),
        )

    def set_battery_port_count(self, count: int) -> None:
        """Set the number of battery ports (from M714 NPrt).

        Triggers entity list rebuild to include per-battery stack
        entities for ports 1..count.
        """
        if count == self._battery_port_count:
            return
        self._battery_port_count = max(1, count)
        self._rebuild_entity_lists()
        logger.info(
            "Battery port count set to %d — %d entities active",
            self._battery_port_count, len(self._entities),
        )

    async def sync_groups(self, db: aiosqlite.Connection) -> None:
        """Sync disabled entity slugs from publishing group settings.

        Reads the DB, rebuilds entity lists, and triggers re-discovery
        if the set changed.
        """
        from franklinwh_bridge.publish.promoted_points import build_promoted_entities
        from franklinwh_bridge.store.db import get_disabled_entity_slugs

        new_disabled = await get_disabled_entity_slugs(db)
        new_promoted = await build_promoted_entities(db, self._gateway_id)
        new_slugs = {e.slug for e in new_promoted}
        old_slugs = {e.slug for e in self._promoted_entities}
        if new_disabled == self._disabled_slugs and new_slugs == old_slugs:
            return  # no change

        # Tombstone promoted points that were un-promoted (removed from groups).
        removed_promoted = [e for e in self._promoted_entities if e.slug not in new_slugs]
        old_count = len(self._entities)
        self._disabled_slugs = new_disabled
        self._promoted_entities = new_promoted
        self._rebuild_entity_lists()
        if removed_promoted:
            self._removed_entities = self._removed_entities + removed_promoted
        logger.info(
            "Publishing groups synced: %d entities active (%d promoted, %d disabled, was %d)",
            len(self._entities),
            len(new_promoted),
            len(new_disabled),
            old_count,
        )

    def set_command_handler(self, handler: CommandHandler) -> None:
        self._command_handler = handler

    def set_power_limits(self, max_charge_w: int, max_discharge_w: int) -> None:
        """Override the battery_command_power entity max_val from hardware.

        Triggers re-discovery so HA picks up the new slider range.
        """
        max_w = max(max_charge_w, max_discharge_w)
        self._max_val_overrides["battery_command_power"] = float(max_w)
        logger.info(
            "Power entity max updated to %dW from hardware nameplate", max_w,
        )
        # Trigger re-discovery on next loop tick
        self._state.discovery_published = False

    # ── Multi-gateway device management ───────────────────────

    def register_device(
        self,
        gateway_id: str,
        device_info: DeviceInfo,
        command_handler: CommandHandler | None = None,
        ac_type: int = 0,
    ) -> GatewayDevice:
        """Register a gateway device for MQTT publishing.

        Creates a per-gateway device entry with its own entity list,
        discovery state, and command handler binding.
        """
        dev = GatewayDevice(
            gateway_id=gateway_id,
            device_info=device_info,
            command_handler=command_handler,
            ac_type=ac_type,
            disabled_slugs=set(self._disabled_slugs),
            max_val_overrides=dict(self._max_val_overrides),
        )
        dev.rebuild_entities()
        self._devices[gateway_id] = dev
        # Trigger discovery on next loop tick
        self._state.discovery_published = False
        logger.info(
            "Registered MQTT device: %s (serial=%s, %d entities)",
            gateway_id, device_info.short_id, len(dev.entities or []),
        )
        return dev

    async def unregister_device(
        self, gateway_id: str, tombstone: bool = True,
    ) -> None:
        """Unregister a gateway device, optionally tombstoning its entities.

        When ``tombstone=True``, queues empty retained payloads for all
        the device's discovery topics so HA removes its entities.
        """
        dev = self._devices.pop(gateway_id, None)
        if dev is None:
            return

        if tombstone and dev.device_info and dev.entities:
            sid = dev.device_info.short_id
            for entity in dev.entities:
                topic = entity.discovery_topic(sid)
                msg = MqttMessage(topic=topic, payload="", retain=True)
                try:
                    self._queue.put_nowait(msg)
                except asyncio.QueueFull:
                    break
            # Also tombstone availability
            avail_topic = f"{TOPIC_PREFIX}/{sid}/availability"
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(
                    MqttMessage(
                        topic=avail_topic, payload="offline", retain=True,
                    )
                )
            logger.info(
                "Unregistered + tombstoned MQTT device: %s (%d entities)",
                gateway_id, len(dev.entities),
            )
        else:
            logger.info("Unregistered MQTT device: %s", gateway_id)

    def get_device(self, gateway_id: str) -> GatewayDevice | None:
        return self._devices.get(gateway_id)

    @property
    def registered_devices(self) -> list[str]:
        return list(self._devices.keys())

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        """Publish HA Discovery config for all registered entities.

        Handles both the legacy single-device and multi-gateway devices.
        Tombstones removed entities so HA deletes stale entries.
        """
        from franklinwh_bridge import __version__

        total_published = 0

        # Publish for the legacy single-device (backward compatible)
        if self._device_info:
            total_published += await self._publish_device_discovery(
                client, self._device_info, self._entities,
                self._removed_entities, self._max_val_overrides,
                __version__,
            )

        # Publish for each registered multi-gateway device
        for dev in self._devices.values():
            if not dev.device_info or not dev.entities:
                continue
            total_published += await self._publish_device_discovery(
                client, dev.device_info, dev.entities,
                dev.removed_entities, dev.max_val_overrides or {},
                __version__,
            )
            dev.discovery_published = True

        if total_published:
            logger.info(
                "Published HA Discovery for %d entities (%d devices)",
                total_published,
                1 + len(self._devices) if self._device_info else len(self._devices),
            )
        self._state.discovery_published = True

    async def _publish_device_discovery(
        self,
        client: aiomqtt.Client,
        device_info: DeviceInfo,
        entities: list[EntityDef],
        removed: list[EntityDef] | None,
        overrides: dict[str, float],
        app_version: str,
    ) -> int:
        """Publish discovery for one device. Returns count published."""
        short_id = device_info.short_id

        # Tombstone removed entities
        if removed:
            for entity in removed:
                topic = entity.discovery_topic(short_id)
                await client.publish(topic, b"", retain=True)

        for entity in entities:
            topic = entity.discovery_topic(short_id)
            payload = build_discovery_payload(
                entity, device_info, app_version=app_version,
            )
            if entity.slug in overrides:
                payload["max"] = overrides[entity.slug]
            await client.publish(topic, json.dumps(payload), retain=True)

        return len(entities)

    async def _publish_availability(
        self, client: aiomqtt.Client, online: bool,
    ) -> None:
        status = "online" if online else "offline"
        # Legacy single device
        if self._device_info:
            topic = f"{TOPIC_PREFIX}/{self._device_info.short_id}/availability"
            await client.publish(topic, status, retain=True)
        # Multi-gateway devices
        for dev in self._devices.values():
            if dev.device_info:
                topic = f"{TOPIC_PREFIX}/{dev.device_info.short_id}/availability"
                await client.publish(topic, status, retain=True)

    async def queue_sample(self, sample: Sample) -> None:
        """Queue per-entity state messages from a poller sample.

        If a per-gateway device is registered for the sample's gateway_id,
        uses that device's entity list and short_id. Otherwise falls back
        to the default single-device (backward compatible).
        """
        gw_id = getattr(sample, "gateway_id", "default")
        dev = self._devices.get(gw_id)

        if dev and dev.device_info and dev.entities:
            # Multi-gateway path: use per-gateway device
            points = dict(sample.points)
            if dev.command_handler:
                points.update(dev.command_handler.virtual_points)
            short_id = dev.device_info.short_id
            entities = dev.entities
        elif self._device_info:
            # Single-device fallback (backward compatible)
            points = dict(sample.points)
            if self._command_handler:
                points.update(self._command_handler.virtual_points)
            short_id = self._device_info.short_id
            entities = self._entities
        else:
            return

        for entity in entities:
            if not entity.stat_key:
                continue
            value = points.get(entity.stat_key)
            if value is None:
                continue

            topic = entity.state_topic(short_id)
            msg = MqttMessage(
                topic=topic,
                payload=entity.format_value(value),
                retain=True,
            )
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning(
                    "MQTT queue full, dropping message for %s", entity.slug,
                )
                break

    async def publish_command_state(self) -> None:
        """Immediately publish command handler virtual points (no poll wait)."""
        if not self._device_info or not self._command_handler:
            return
        points = self._command_handler.virtual_points
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
            raw = message.payload
            payload = raw.decode() if isinstance(raw, bytes) else str(raw)
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
                    subscribed_commands = False

                    try:
                        while not self._stop_event.is_set():
                            if (
                                not subscribed_commands
                                and self._device_info
                                and self._command_handler
                            ):
                                sid = self._device_info.short_id
                                cmd_topic = f"{TOPIC_PREFIX}/{sid}/control/+/set"
                                await client.subscribe(cmd_topic)
                                listener_task = asyncio.create_task(
                                    self._subscribe_listener(client)
                                )
                                subscribed_commands = True
                                logger.info("Subscribed to command topics: %s", cmd_topic)

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
        """Send empty retained payloads to ALL discovery topics (tombstones).

        Uses BRIDGE_ENTITIES (the full registry) rather than the filtered
        ``self._entities`` list, so orphaned entities from previous phase
        configurations are also cleaned up.
        """
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
                for entity in BRIDGE_ENTITIES:
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
