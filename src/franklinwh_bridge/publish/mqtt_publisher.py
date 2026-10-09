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

#: Seconds a gateway may publish before registration without being warned about
#: (samples routinely arrive just before start → sync registers it).
_UNREGISTERED_GRACE_S = 30.0


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
        """Rebuild entity lists from phase + battery port + group filters.

        Control entities are skipped when the gateway has no command handler.
        A mock is the case that matters: control is deliberately not wired for
        one, so its select/number controls had no state topic to publish and no
        command to accept — they sat at "unknown" forever. HA showed a full
        Controls card of dead sliders, which reads as broken rather than as
        not-applicable.
        """
        disabled = self.disabled_slugs or set()
        ac = self.ac_type
        nport = self.battery_port_count
        commandable = self.command_handler is not None
        self.entities = [
            e for e in BRIDGE_ENTITIES
            if (e.phase is None or e.phase <= ac + 1)
            and (e.battery_port is None or e.battery_port <= nport)
            and e.slug not in disabled
            and (commandable or not e.is_control)
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


def _slug(value: str) -> str:
    """Lowercase, alphanumerics and underscores only — safe in an MQTT topic
    and in an HA unique_id. Collapses runs so "Mock GW 1" → "mock_gw_1"."""
    out = "".join(c.lower() if c.isalnum() else "_" for c in value)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "gateway"


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

        The default gateway keeps the historic serial-only id so existing Home
        Assistant entities are preserved — do not change this branch.

        Additional gateways use their slugified gateway_id, which is already
        unique per install. Two earlier attempts were worse:

        - ``{gateway_id}_{serial_tail}`` mangled mock serials. _serial_tail
          slices the last 8 characters, fine for 10060006A02F00000001 →
          00000001, but "MOCK-MOCK GW 1" → "OCK GW 1", giving the topic
          ``franklinwh/Mock GW 1_OCK GW 1/...``.
        - Either form kept the gateway_id's spaces in the topic. Legal in MQTT,
          but it breaks shell and CLI use and reads as a typo.

        Safe to change: no non-default gateway has ever been registered
        (register_device had no callers), so there are no existing entities to
        orphan.

        Slugs could in principle collide ("GW 1" and "GW-1" both → gw_1). That
        needs deliberately near-identical names, and the alternative was a
        mangled serial in every topic.
        """
        if self.gateway_id and self.gateway_id != "default":
            return _slug(self.gateway_id)
        return self._serial_tail

    def ha_device_block(self, app_version: str = "") -> dict:
        name = self.name or f"FranklinWH {self.short_id}"

        # Name this bridge explicitly as the producer.
        #
        # Several projects publish HA discovery for the SAME physical aGate —
        # this bridge, FWHAI, the local bridge — and all of them namespace under
        # `franklinwh_<id>_<key>`. On a shared broker their device identifiers
        # collide and HA merges them into one device whose metadata is written
        # by whoever published last. When that happens the only way to tell what
        # produced an entity is this field, so it must say so unambiguously
        # rather than burying it in parentheses after the aGate's own firmware.
        # HA renders sw_version as "Firmware" and hw_version as "Hardware", so
        # the two facts get a line each instead of one long concatenation:
        #
        #   Firmware: Modbus Bridge v0.1.0
        #   Hardware: aGate V10R01B04D00
        #
        # Calling the aGate's own firmware "Hardware" is a slight stretch, but
        # HA offers only these two slots and the alternative — dropping the
        # producer — is what made a merged device undiagnosable in the first
        # place. Both facts stay visible and neither line wraps.
        sw = f"Modbus Bridge v{app_version}" if app_version else self.firmware
        hw = f"aGate {self.firmware}" if (app_version and self.firmware) else ""
        # Device identifier mirrors short_id's namespacing so duplicate serials
        # don't merge into one HA device.
        if self.gateway_id and self.gateway_id != "default":
            identifier = _slug(f"franklinwh_{self.gateway_id}_{self.serial}")
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
        if hw:
            block["hw_version"] = hw
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
        self._phase_view: str = "both"  # both | aggregate | per_phase (Topology B)
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
        # phase_view (Topology B): 'aggregate' suppresses the per-leg L1/L2/L3
        # entities — but only on a multi-phase unit. A single-phase aGate
        # (ac_type 0) keeps its L1 set, since that IS its real data.
        if self._phase_view == "aggregate" and ac_type >= 1:
            new_entities = [e for e in new_entities if e.phase is None]
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

    def set_phase_view(self, view: str) -> None:
        """Set the per-gateway phase-view preference (Topology B).

        'both' (default) publishes aggregate + per-leg entities. 'aggregate'
        suppresses the per-leg L1/L2/L3 entities on a multi-phase unit.
        'per_phase' publishes the per-leg set (dashboard de-emphasises the
        aggregate). Triggers an entity-list rebuild + discovery re-publish.
        """
        view = view if view in ("both", "aggregate", "per_phase") else "both"
        if view == self._phase_view:
            return
        self._phase_view = view
        self._rebuild_entity_lists()
        logger.info(
            "Phase view set to '%s' — %d entities active", view, len(self._entities)
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

    @property
    def gateway_id(self) -> str:
        """The gateway this publisher owns via the legacy single-device path."""
        return self._gateway_id

    def devices(self) -> list[str]:
        """Gateway ids with a registered MQTT device (snapshot, safe to mutate
        the registry while iterating the result)."""
        return list(self._devices)

    def get_device(self, gateway_id: str) -> GatewayDevice | None:
        return self._devices.get(gateway_id)

    @property
    def registered_devices(self) -> list[str]:
        return list(self._devices.keys())

    def _has_discoverable(self) -> bool:
        """Anything to announce to HA — the default device OR any registered one.

        Gating discovery on the default gateway alone meant that with the
        default unconfigured or offline, no gateway was ever announced: a mock
        added to explore the bridge published samples HA never had entities
        for.
        """
        return bool(self._device_info) or any(
            d.device_info for d in self._devices.values()
        )

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

    def _warn_unregistered(self, gw_id: str) -> None:
        """Say once per gateway that its samples aren't reaching MQTT.

        Once, not every poll — at a 5s interval this would be ~17k lines a day
        per gateway and would bury everything else. But not silent either: a
        gateway quietly missing from HA is exactly the kind of gap that gets
        mistaken for working.
        """
        if not hasattr(self, "_warned_unregistered"):
            self._warned_unregistered: set[str] = set()
            self._unregistered_since: dict[str, float] = {}
        if gw_id in self._warned_unregistered:
            return
        # A gateway's first samples arrive a moment before it is registered
        # (start, then sync). Only warn if it is STILL unregistered after a
        # grace period — otherwise every healthy add/start logged this.
        first = self._unregistered_since.setdefault(gw_id, time.monotonic())
        if time.monotonic() - first < _UNREGISTERED_GRACE_S:
            return
        self._warned_unregistered.add(gw_id)
        logger.warning(
            "Gateway %s has no registered MQTT device — its samples are NOT "
            "published to Home Assistant. (Previously they were published under "
            "the default gateway's topics, overwriting its values.)", gw_id,
        )

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
        elif self._device_info and gw_id == self._gateway_id:
            # Single-device fallback — ONLY for the gateway this publisher was
            # configured with.
            points = dict(sample.points)
            if self._command_handler:
                points.update(self._command_handler.virtual_points)
            short_id = self._device_info.short_id
            entities = self._entities
        else:
            # A sample from a gateway with no registered device. This used to
            # fall through to the branch above, which published it under the
            # DEFAULT gateway's topics — so a second gateway impersonated the
            # first and its values overwrote the real ones. Observed live: a
            # mock gateway's SoC (28%) alternating with a real aGate's (82%) on
            # franklinwh/<serial>/battery/battery_soc, flip-flopping the HA
            # entity every poll.
            #
            # Dropping is the lesser evil: a missing entity is visibly missing,
            # whereas a wrong value attributed to the right device is trusted.
            # register_device() is implemented but not yet wired at startup, so
            # today this path means "extra gateways don't reach HA" — which is
            # what the docs already claimed was happening.
            self._warn_unregistered(gw_id)
            return

        for entity in entities:
            if not entity.stat_key:
                continue
            value = points.get(entity.stat_key)
            # A key that is present but None is a known "unknown" (reset it in
            # HA); an absent key just wasn't read this poll (keep the last value).
            if value is None and not (
                entity.reset_when_unknown and entity.stat_key in points
            ):
                continue

            topic = entity.state_topic(short_id)
            msg = MqttMessage(
                topic=topic,
                payload="None" if value is None else entity.format_value(value),
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

    def _command_targets(self) -> dict[str, CommandHandler]:
        """Device short_id -> the command handler its control topics drive.

        Every commandable device, not only the default gateway: additional
        gateways (a second aGate, a mock) got control entities in HA that were
        never subscribed, so their controls silently did nothing.
        """
        targets: dict[str, CommandHandler] = {}
        if self._device_info and self._command_handler:
            targets[self._device_info.short_id] = self._command_handler
        for dev in self._devices.values():
            if dev.device_info and dev.command_handler:
                targets[dev.device_info.short_id] = dev.command_handler
        return targets

    async def _handle_mqtt_message(self, message: aiomqtt.Message) -> None:
        """Dispatch an incoming MQTT command to the gateway its topic names.

        Topic: ``franklinwh/<short_id>/control/<slug>/set``. Routed by the
        short_id — this used to send every command to the DEFAULT gateway's
        handler whatever device it was addressed to.
        """
        parts = str(message.topic).split("/")
        if not (len(parts) >= 5 and parts[2] == "control" and parts[-1] == "set"):
            return
        handler = self._command_targets().get(parts[1])
        if handler is None:
            logger.warning("Command for unknown device %s ignored: %s", parts[1], message.topic)
            return
        raw = message.payload
        payload = raw.decode() if isinstance(raw, bytes) else str(raw)
        await handler.handle_command(parts[3], payload)

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
                    if self._has_discoverable() and not self._state.discovery_published:
                        await self._publish_discovery(client)

                    listener_task = None
                    subscribed: set[str] = set()  # short_ids whose control topics we hold

                    try:
                        while not self._stop_event.is_set():
                            # Subscribe each commandable device as it appears
                            # (gateways register after we connect).
                            for sid in self._command_targets().keys() - subscribed:
                                cmd_topic = f"{TOPIC_PREFIX}/{sid}/control/+/set"
                                await client.subscribe(cmd_topic)
                                subscribed.add(sid)
                                logger.info("Subscribed to command topics: %s", cmd_topic)
                                if listener_task is None:
                                    listener_task = asyncio.create_task(
                                        self._subscribe_listener(client)
                                    )

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

                            if self._has_discoverable() and not self._state.discovery_published:
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
