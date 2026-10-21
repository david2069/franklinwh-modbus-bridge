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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiomqtt

if TYPE_CHECKING:
    import aiosqlite

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.publish.entities import (
    BRIDGE_ENTITIES,
    DEFAULT_DISCOVERY_PREFIX,
    DEFAULT_TOPIC_PREFIX,
    EntityDef,
)

logger = logging.getLogger(__name__)

#: This integration's name in the shared HA device convention
#: (docs/ha-device-identity.md): device "FranklinWH Modbus <short serial>",
#: identifier "<prefix>_modbus_<SERIAL>". The integration part is what keeps it
#: from ever matching FWHAI's or the Local Bridge's device for the same aGate.
INTEGRATION_LABEL = "Modbus"
INTEGRATION_ID = "modbus"

#: Dropped connections within this many seconds that suggest another client is
#: using our client id (#118): the broker closes the older of the two.
_FLAP_WINDOW_S = 120.0
_FLAP_COUNT = 3

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

    def ha_device_block(
        self, app_version: str = "", prefix: str = DEFAULT_TOPIC_PREFIX,
    ) -> dict:
        # One HA device per INTEGRATION (owner decision 2026-10-15): "FranklinWH
        # Modbus <short serial>", next to "FranklinWH Direct …" (Local Bridge)
        # and "FranklinWH Cloud …". Same aGate, different integrations, so
        # different devices — never one merged device. See
        # docs/ha-device-identity.md.
        # The suffix is the aGate's short serial for EVERY gateway — not the
        # gateway id, which extra gateways use as their topic id (#128). A mock
        # has no real serial; it keeps its id and a visible "(mock)" so nobody
        # wires an automation to synthetic data.
        if self.firmware == "MOCK" or "(mock)" in (self.manufacturer or ""):
            name = f"FranklinWH {INTEGRATION_LABEL} {self.short_id} (mock)"
        else:
            name = f"FranklinWH {INTEGRATION_LABEL} {self._serial_tail}"

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
            identifier = _slug(f"{prefix}_{INTEGRATION_ID}_{self.gateway_id}_{self.serial}")
        else:
            identifier = f"{prefix}_{INTEGRATION_ID}_{self.serial}"
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
    prefix: str = DEFAULT_TOPIC_PREFIX,
) -> dict:
    """Build an HA MQTT Discovery config payload for a single entity."""
    short_id = device_info.short_id
    avail_topic = f"{prefix}/{short_id}/availability"

    payload: dict = {
        "unique_id": entity.unique_id(short_id, prefix),
        "name": entity.name,
        "state_topic": entity.state_topic(short_id, prefix),
        "availability_topic": avail_topic,
        "device": device_info.ha_device_block(app_version, prefix),
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

    cmd_topic = entity.command_topic(short_id, prefix)
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

    #: The default gateway's "Publish to Home Assistant" is off (#96): its
    #: samples are dropped without the "unregistered gateway" warning. A class
    #: attribute so publishers built without __init__ (tests) read it as off.
    _default_suppressed: bool = False
    #: Set by ``stop(offline=True)``: the run loop says "offline" on its way out.
    _offline_on_stop: bool = False
    #: Awaited after discovery is first published on a connection (main.py
    #: records it, #87). A class attribute for the same reason as above.
    on_published: Callable[[], Awaitable[None]] | None = None

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        gateway_id: str = "default",
        client_id: str = "franklinwh_bridge",
        qos: int = 0,
        topic_prefix: str = DEFAULT_TOPIC_PREFIX,
        discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
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
        # Set by Unpublish (#131): the run loop must not put the entities straight
        # back. Cleared by Republish. Persisted by the API (app_config).
        self._discovery_paused = False
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
        self._default_suppressed = False
        self._state.discovery_published = False

    @property
    def default_suppressed(self) -> bool:
        """True while the default gateway's "Publish to Home Assistant" is off."""
        return self._default_suppressed

    def resume_default_device(self) -> None:
        """Publishing is wanted for the default gateway again; it is bound by
        the next ``set_device_info``. Until then an unbound default warns as
        unregistered, as it always did."""
        self._default_suppressed = False

    def unbind_default_device(self, tombstone: bool = True) -> int:
        """Stop publishing the default gateway to HA, live (#96).

        The counterpart of ``set_device_info`` for a gateway whose "Publish to
        Home Assistant" is off. With ``tombstone`` and a device bound, queues an
        empty retained payload for every discovery topic it could have — the
        full BRIDGE_ENTITIES registry plus promoted points, as Unpublish does —
        and for its availability topic, so HA removes the entities rather than
        leaving them unavailable. Then unbinds, so samples and command state
        are no longer queued and its control topics are no longer obeyed.

        Returns the number of tombstones queued. Idempotent: with nothing bound
        it only (re)marks the gateway as suppressed.
        """
        count = 0
        info = self._device_info
        if tombstone and info is not None:
            sid = info.short_id
            seen: set[str] = set()
            for entity in [*BRIDGE_ENTITIES, *self._promoted_entities]:
                topic = self._discovery_topic(entity, sid)
                if topic in seen:
                    continue
                seen.add(topic)
                try:
                    self._queue.put_nowait(MqttMessage(topic=topic, payload="", retain=True))
                except asyncio.QueueFull:
                    logger.warning("MQTT queue full — not every tombstone was queued")
                    break
                count += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(
                    MqttMessage(topic=self._availability_topic(sid), payload="", retain=True)
                )
            logger.info(
                "Default gateway unpublished from Home Assistant: %d discovery "
                "topics tombstoned", count,
            )
        self._device_info = None
        self._command_handler = None
        self._default_suppressed = True
        return count

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
        # Groups are bridge-wide: every registered gateway follows them too.
        # They used to keep the set copied at registration, so switching a
        # group off left a second aGate's entities (its controls) in HA (#204).
        for dev in self._devices.values():
            if dev.disabled_slugs != new_disabled:
                dev.disabled_slugs = set(new_disabled)
                dev.rebuild_entities()
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
                topic = self._discovery_topic(entity, sid)
                msg = MqttMessage(topic=topic, payload="", retain=True)
                try:
                    self._queue.put_nowait(msg)
                except asyncio.QueueFull:
                    break
            # Also tombstone availability
            avail_topic = self._availability_topic(sid)
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

    @property
    def topic_prefix(self) -> str:
        """Topic root and unique_id/device-identifier prefix in use (#90)."""
        return self._topic_prefix

    @property
    def discovery_prefix(self) -> str:
        """HA discovery topic root in use (#90)."""
        return self._discovery_prefix

    def _discovery_topic(self, entity: EntityDef, short_id: str) -> str:
        return entity.discovery_topic(short_id, self._topic_prefix, self._discovery_prefix)

    def _availability_topic(self, short_id: str) -> str:
        return f"{self._topic_prefix}/{short_id}/availability"

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
            if self.on_published is not None:
                # Recorded so a later namespace change knows it needs the
                # confirmed flow (#87). Never allowed to break publishing.
                try:
                    await self.on_published()
                except Exception as exc:
                    logger.debug("on_published hook failed: %s", exc)
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
                topic = self._discovery_topic(entity, short_id)
                await client.publish(topic, b"", retain=True)

        for entity in entities:
            topic = self._discovery_topic(entity, short_id)
            payload = build_discovery_payload(
                entity, device_info, app_version=app_version, prefix=self._topic_prefix,
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
            topic = self._availability_topic(self._device_info.short_id)
            await client.publish(topic, status, retain=True)
        # Multi-gateway devices
        for dev in self._devices.values():
            if dev.device_info:
                topic = self._availability_topic(dev.device_info.short_id)
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
        elif gw_id == self._gateway_id and self._default_suppressed:
            # The default gateway with "Publish to Home Assistant" off (#96):
            # dropped by choice, so no "unregistered gateway" warning.
            return
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

            topic = entity.state_topic(short_id, self._topic_prefix)
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
            topic = entity.state_topic(short_id, self._topic_prefix)
            msg = MqttMessage(topic=topic, payload=entity.format_value(value), retain=True)
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                break

    def _will_topic(self) -> str | None:
        """The availability topic the broker marks "offline" if we vanish (#325).

        MQTT carries one last will per connection, and topics are frozen (D1),
        so it can only be an existing per-gateway availability topic: set when
        exactly one gateway is announced. With several there is no will until a
        bridge-level availability topic is allowed after the identity freeze.
        """
        sids = {d.device_info.short_id for d in self._devices.values() if d.device_info}
        if self._device_info:
            sids.add(self._device_info.short_id)
        if len(sids) != 1:
            return None
        return self._availability_topic(next(iter(sids)))

    def _backoff_delay(self, attempt: int) -> float:
        return min(5.0 * (2 ** attempt), 60.0)

    def _enabled_control_slugs(self) -> set[str]:
        """Control slugs whose publishing group is on (bridge-wide setting)."""
        return {
            e.slug for e in BRIDGE_ENTITIES
            if e.is_control and e.slug not in self._disabled_slugs
        }

    def _command_targets(self) -> dict[str, CommandHandler]:
        """Device short_id -> the command handler its control topics drive.

        Every commandable device, not only the default gateway: additional
        gateways (a second aGate, a mock) got control entities in HA that were
        never subscribed, so their controls silently did nothing.

        Empty while the Control group is off: the entities leave HA, and the
        command topics must stop working too, or anyone who can publish to
        the broker can still command the battery (#204).
        """
        targets: dict[str, CommandHandler] = {}
        if not self._enabled_control_slugs():
            return targets
        if self._device_info and self._command_handler:
            targets[self._device_info.short_id] = self._command_handler
        for dev in self._devices.values():
            if dev.device_info and dev.command_handler:
                targets[dev.device_info.short_id] = dev.command_handler
        return targets

    async def _handle_mqtt_message(self, message: aiomqtt.Message) -> None:
        """Dispatch an incoming MQTT command to the gateway its topic names.

        Topic: ``<topic_prefix>/<short_id>/control/<slug>/set``. Routed by the
        short_id — this used to send every command to the DEFAULT gateway's
        handler whatever device it was addressed to. Only our own namespace is
        acted on: another producer's control topics are not ours to obey.
        """
        parts = str(message.topic).split("/")
        if not (len(parts) >= 5 and parts[2] == "control" and parts[-1] == "set"):
            return
        if parts[0] != self._topic_prefix:
            return
        if parts[3] not in self._enabled_control_slugs():
            # A message already in flight when the group went off, or a slug
            # whose group is off while another control group is on (#204).
            logger.warning("Command refused, its publishing group is off: %s", message.topic)
            return
        handler = self._command_targets().get(parts[1])
        if handler is None:
            logger.warning("Command for unknown device %s ignored: %s", parts[1], message.topic)
            return
        raw = message.payload
        payload = raw.decode() if isinstance(raw, bytes) else str(raw)
        await handler.handle_command(parts[3], payload, requester="mqtt")

    async def _subscribe_listener(self, client: aiomqtt.Client) -> None:
        """Background task: listen for incoming command messages."""
        async for message in client.messages:
            # One bad message must not end the listener: publishing would carry
            # on and HA commands would silently go nowhere (#31).
            try:
                await self._handle_mqtt_message(message)
            except Exception:
                logger.exception("MQTT command handling failed for %s", message.topic)

    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            rewill = False
            will_topic = self._will_topic()
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    identifier=self._client_id,
                    will=(aiomqtt.Will(will_topic, "offline", qos=1, retain=True)
                          if will_topic else None),
                ) as client:
                    self._state.connected = True
                    self._state.last_error = None
                    attempt = 0
                    logger.info("MQTT connected to %s:%d", self._host, self._port)

                    await self._publish_availability(client, online=True)
                    if (self._has_discoverable() and not self._state.discovery_published
                            and not self._discovery_paused):
                        await self._publish_discovery(client)

                    listener_task = None
                    subscribed: set[str] = set()  # short_ids whose control topics we hold

                    try:
                        while not self._stop_event.is_set():
                            # Gateways register after we connect: once the will
                            # topic is known (or changes), reconnect cleanly to
                            # carry it. A clean disconnect doesn't fire the will.
                            if self._will_topic() != will_topic:
                                rewill = True
                                break
                            # Subscribe each commandable device as it appears
                            # (gateways register after we connect), and drop
                            # any that stops being one: unregistered, or the
                            # Control group switched off (#204).
                            targets = self._command_targets().keys()
                            for sid in subscribed - targets:
                                cmd_topic = f"{self._topic_prefix}/{sid}/control/+/set"
                                await client.unsubscribe(cmd_topic)
                                subscribed.discard(sid)
                                logger.info("Unsubscribed from command topics: %s", cmd_topic)
                            for sid in targets - subscribed:
                                cmd_topic = f"{self._topic_prefix}/{sid}/control/+/set"
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

                            if (self._has_discoverable() and not self._state.discovery_published
                                    and not self._discovery_paused):
                                await self._publish_discovery(client)
                                await self._publish_availability(client, online=True)
                        if self._offline_on_stop:
                            # Publishing switched off (#96): HA shows the
                            # entities unavailable, not frozen at a last value.
                            await self._publish_availability(client, online=False)
                    finally:
                        if listener_task:
                            listener_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await listener_task

            except aiomqtt.MqttError as exc:
                was_connected = self._state.connected
                self._state.connected = False
                self._state.last_error = str(exc)
                logger.error("MQTT error: %s", exc)
                if was_connected:
                    self._note_dropped()
            except Exception as exc:
                self._state.connected = False
                self._state.last_error = str(exc)
                logger.error("MQTT unexpected error: %s", exc)

            if rewill and not self._stop_event.is_set():
                logger.info("MQTT reconnecting to set the last will (%s)",
                            self._will_topic() or "none")
                continue
            if not self._stop_event.is_set():
                delay = self._backoff_delay(attempt)
                attempt += 1
                logger.info("MQTT reconnect in %.0fs", delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    def _note_dropped(self) -> None:
        """Warn when established connections keep being dropped (#118).

        A broker keeps one connection per client id; a second client with the
        same id knocks the first off, which reconnects and knocks the second
        off, in a loop. Said once per burst, not on every drop.
        """
        now = time.monotonic()
        drops = [t for t in getattr(self, "_drops", []) if now - t < _FLAP_WINDOW_S]
        drops.append(now)
        self._drops = drops
        if len(drops) == _FLAP_COUNT:
            logger.warning(
                "MQTT connection dropped %d times in %.0fs — another client may be "
                "using client id %s on this broker (#118)",
                len(drops), _FLAP_WINDOW_S, self._client_id,
            )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("MQTT publisher started")

    async def stop(self, offline: bool = False) -> None:
        """Stop the connection loop.

        ``offline`` first lets the loop publish "offline" availability for
        every bound device (bounded wait), instead of cancelling it mid-flight.
        """
        self._offline_on_stop = offline
        self._stop_event.set()
        if self._task:
            if offline and not self._task.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self._task), timeout=3.0)
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._state.connected = False
        self._offline_on_stop = False
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
            topic_prefix=config.get("topic_prefix") or DEFAULT_TOPIC_PREFIX,
            discovery_prefix=config.get("discovery_prefix") or DEFAULT_DISCOVERY_PREFIX,
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

    def set_broker(self, config: dict) -> None:
        """Point at a broker without starting or stopping anything (#103).

        For a caller that stops and starts the loop itself, as
        ``/api/mqtt/reconnect`` does; ``reconfigure`` restarts on its own.
        """
        self._host = config.get("host", self._host)
        self._port = config.get("port", self._port)
        self._username = config.get("username", self._username)
        self._password = config.get("password", self._password)
        self._state.discovery_published = False

    @property
    def is_running(self) -> bool:
        """True while the connection loop runs (connected or retrying)."""
        return bool(self._task and not self._task.done())

    def bound_short_ids(self) -> set[str]:
        """Short ids of every device bound now (default + registered)."""
        sids = {self._device_info.short_id} if self._device_info else set()
        sids |= {d.device_info.short_id for d in self._devices.values() if d.device_info}
        return sids

    def switch_namespace(self, topic_prefix: str) -> None:
        """Use a new namespace (#87) — topics, unique_ids, device identifiers
        and the command subscription. Call while stopped; the next connection
        subscribes and republishes discovery under it.

        State already queued for the old namespace is dropped, so nothing is
        written there after the switch.
        """
        old = self._topic_prefix
        self._topic_prefix = topic_prefix
        kept: list[MqttMessage] = []
        while not self._queue.empty():
            msg = self._queue.get_nowait()
            if not msg.topic.startswith(f"{old}/"):
                kept.append(msg)
        for msg in kept:
            self._queue.put_nowait(msg)
        self._state.discovery_published = False
        for dev in self._devices.values():
            dev.discovery_published = False

    async def tombstone_own_discovery(
        self, old_prefix: str, short_ids: set[str], window_s: float = 2.0,
    ) -> dict[str, int]:
        """Remove this bridge's retained discovery configs under ``old_prefix``.

        Reads what is retained on every discovery topic this bridge publishes or
        could have published (full registry + promoted points, per device) and
        sends an empty retained payload ONLY where the config is ours
        (``namespace.is_ours``). Another producer that shares the namespace —
        FWHAI under ``franklinwh`` — keeps its entities. Availability and state
        topics are not touched.

        Raises if the broker can't be reached; then nothing has been removed.
        """
        from franklinwh_bridge.publish.namespace import is_ours

        topics: list[str] = []
        for sid in sorted(short_ids):
            for entity in [*BRIDGE_ENTITIES, *self._promoted_entities]:
                topic = entity.discovery_topic(sid, old_prefix, self._discovery_prefix)
                if topic not in topics:
                    topics.append(topic)
        wanted = set(topics)
        retained: dict[str, bytes | str] = {}
        removed = 0
        # A client id of its own: reusing ours would knock the main connection
        # off the broker (#118).
        async with aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=f"{self._client_id}_ns",
            timeout=10,
        ) as client:
            for i in range(0, len(topics), 100):
                await client.subscribe([(t, 0) for t in topics[i:i + 100]])

            async def _collect() -> None:
                async for message in client.messages:
                    topic = str(message.topic)
                    # Retained only: a live publish during the window is
                    # someone else at work, not what is stored.
                    if topic in wanted and getattr(message, "retain", False) and message.payload:
                        retained[topic] = message.payload

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_collect(), timeout=window_s)
            for topic, payload in retained.items():
                if is_ours(payload):
                    await client.publish(topic, b"", retain=True)
                    removed += 1
        counts = {
            "removed_ours": removed,
            "left_foreign": len(retained) - removed,
            "not_found": len(topics) - len(retained),
        }
        logger.info(
            "Namespace %s: %d of this bridge's discovery configs removed, %d of "
            "other producers left, %d topics empty (%d devices)",
            old_prefix, counts["removed_ours"], counts["left_foreign"],
            counts["not_found"], len(short_ids),
        )
        return counts

    def request_discovery_republish(self) -> None:
        """Flag discovery for re-publish on the next connection cycle.

        Also ends an Unpublish pause (#131): Republish is how entities come back.
        """
        self._discovery_paused = False
        self._state.discovery_published = False

    def published_discovery(self) -> tuple[set[str], set[str]]:
        """(short ids, discovery topics) this publisher currently publishes.

        For the stale-config sweep (publish/cleanup.py): anything of ours on
        the broker outside this set is a leftover.
        """
        sids: set[str] = set()
        topics: set[str] = set()
        if self._device_info:
            sid = self._device_info.short_id
            sids.add(sid)
            for e in list(self._entities) + list(self._promoted_entities):
                topics.add(self._discovery_topic(e, sid))
        for dev in self._devices.values():
            if dev.device_info:
                sid = dev.device_info.short_id
                sids.add(sid)
                for e in dev.entities or []:
                    topics.add(self._discovery_topic(e, sid))
        return sids, topics

    @property
    def discovery_paused(self) -> bool:
        """True after Unpublish until Republish (#131)."""
        return self._discovery_paused

    def set_discovery_paused(self, paused: bool) -> None:
        self._discovery_paused = bool(paused)

    async def unpublish_discovery(self) -> int:
        """Send empty retained payloads to ALL discovery topics (tombstones).

        Uses BRIDGE_ENTITIES (the full registry) rather than the filtered
        ``self._entities`` list, so orphaned entities from previous phase
        configurations are also cleaned up.
        """
        # Every gateway's device, not only the primary's — extra gateways'
        # entities were left in Home Assistant. And pause first: the run loop
        # republished within a second, so Unpublish undid itself (#131).
        self._discovery_paused = True
        sids: list[str] = []
        if self._device_info:
            sids.append(self._device_info.short_id)
        for dev in self._devices.values():
            if dev.device_info and dev.device_info.short_id not in sids:
                sids.append(dev.device_info.short_id)
        if not sids:
            return 0

        count = 0
        try:
            async with aiomqtt.Client(
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            ) as client:
                for short_id in sids:
                    for entity in BRIDGE_ENTITIES:
                        topic = self._discovery_topic(entity, short_id)
                        await client.publish(topic, b"", retain=True)
                        count += 1
                    avail_topic = self._availability_topic(short_id)
                    await client.publish(avail_topic, b"", retain=True)
        except Exception as exc:
            logger.error("Failed to unpublish discovery: %s", exc)
            raise

        logger.info("Unpublished %d discovery topics", count)
        self._state.discovery_published = False
        return count
