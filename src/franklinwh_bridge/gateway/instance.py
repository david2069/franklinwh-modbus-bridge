"""GatewayInstance — encapsulates everything for one aGate connection.

Each instance owns a controller, poller, command handler, modbus lock,
and per-gateway sample bus.  The instance lifecycle mirrors what main.py's
_init_poller() does for the single "default" gateway, but is reusable
for any number of gateways.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from franklinwh_bridge.modbus.catalog import (
    capture_catalog,
    extract_catalog_from_controller,
)
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import Sample, SampleBus
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo
from franklinwh_bridge.store.db import (
    load_control_state,
    log_control_event,
    save_control_state,
    update_gateway,
)

logger = logging.getLogger(__name__)


@dataclass
class GatewayConfig:
    """Configuration for a single gateway."""

    gateway_id: str
    name: str
    host: str
    port: int = 502
    unit_id: int = 1
    poll_interval: int = 10
    description: str = ""
    enabled: bool = True


@dataclass
class GatewayStatus:
    """Runtime status for a gateway instance."""

    connected: bool = False
    polling: bool = False
    health: str = "unknown"  # "connected", "tcp_only", "unreachable", "unknown"
    last_poll_ts: float | None = None
    last_error: str | None = None
    serial: str | None = None
    model: str | None = None
    firmware: str | None = None
    ac_type: int = 0


class GatewayInstance:
    """Manages one aGate's Modbus connection, poller, and command handler.

    Call ``start()`` to connect and begin polling, ``stop()`` to gracefully
    shut down.  All Modbus operations are serialised through the per-gateway
    ``modbus_lock``.
    """

    def __init__(
        self,
        config: GatewayConfig,
        db: aiosqlite.Connection,
        global_bus: SampleBus,
        stats: Any | None = None,
    ) -> None:
        self.config = config
        self.gateway_id = config.gateway_id
        self._db = db
        self._global_bus = global_bus
        self._stats = stats

        # Per-gateway resources (created on start)
        self.controller: Any | None = None
        self.poller: ModbusPoller | None = None
        self.command_handler: CommandHandler | None = None
        self.modbus_lock = asyncio.Lock()
        self.sample_bus = SampleBus()  # per-gateway bus
        self.device_info: DeviceInfo | None = None
        self.status = GatewayStatus()
        self.reader_fn: Any | None = None  # for POST /api/models/refresh

        self._init_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self.status.polling and self.poller is not None

    async def start(self) -> None:
        """Create controller, connect, discover device, start poller."""
        try:
            from franklinwh_modbus import FranklinWHController
        except ImportError:
            logger.error(
                "Gateway %s: franklinwh-modbus not installed", self.gateway_id
            )
            return

        cfg = self.config
        self.controller = FranklinWHController(
            ip_address=cfg.host, port=cfg.port, unit_id=cfg.unit_id,
        )

        self.poller = ModbusPoller(
            controller=self.controller,
            sample_bus=self.sample_bus,
            gateway_id=self.gateway_id,
            poll_interval=cfg.poll_interval,
            stats=self._stats,
            modbus_lock=self.modbus_lock,
        )

        # Fan-in: forward per-gateway samples to the global bus
        self.sample_bus.subscribe(self._forward_to_global)

        self.command_handler = CommandHandler(
            self.controller,
            self._db,
            points_getter=self._get_cached_points,
            modbus_lock=self.modbus_lock,
        )

        # Run the connect + discover sequence in background
        self._init_task = asyncio.create_task(self._init_and_poll())

    async def _init_and_poll(self) -> None:
        """Connect, discover device info, release stale commands, start polling."""
        try:
            connected = await asyncio.to_thread(self.controller.connect)
            if not connected:
                self.status.health = "unreachable"
                self.status.last_error = "Connection failed"
                logger.warning(
                    "Gateway %s: connection failed to %s:%d",
                    self.gateway_id, self.config.host, self.config.port,
                )
                return

            self.status.connected = True
            self.status.health = "connected"

            # Discover device info (serial, model, firmware)
            await self._discover_device()

            # Log startup HW state
            await self._log_startup_state()

            # Release stale commands from previous session
            await self._release_stale_commands()

            # Detect AC wiring type
            try:
                await asyncio.to_thread(self.poller._detect_ac_type)
                self.status.ac_type = self.poller.ac_type
                await update_gateway(
                    self._db, self.gateway_id, ac_type=self.status.ac_type,
                )
            except Exception as exc:
                logger.warning("Gateway %s: AC type detection failed: %s", self.gateway_id, exc)

            # Capture SunSpec catalog
            try:
                dev_info = await asyncio.to_thread(
                    extract_catalog_from_controller, self.controller,
                )
                cat_hash, _ = await capture_catalog(
                    dev_info, self._db, self.gateway_id,
                )
                logger.info(
                    "Gateway %s: SunSpec catalog captured (%d models, hash=%s)",
                    self.gateway_id,
                    len(dev_info.get("models", {})),
                    cat_hash,
                )
            except Exception as exc:
                logger.warning("Gateway %s: catalog capture failed: %s", self.gateway_id, exc)

            # Wire reader_fn for POST /api/models/refresh
            ctrl = self.controller

            async def _reader_fn():
                try:
                    info = await asyncio.to_thread(
                        extract_catalog_from_controller, ctrl,
                    )
                    return info, None
                except Exception as exc:
                    return None, str(exc)

            self.reader_fn = _reader_fn

            # Disconnect before poller takes over (poller reconnects with lock)
            await asyncio.to_thread(self.controller.disconnect)

            # Start polling
            await self.poller.start()
            self.status.polling = True
            logger.info(
                "Gateway %s started: %s (serial=%s)",
                self.gateway_id,
                self.config.name,
                self.status.serial or "unknown",
            )

        except Exception as exc:
            self.status.health = "unreachable"
            self.status.last_error = str(exc)
            logger.error("Gateway %s init failed: %s", self.gateway_id, exc)

    async def _discover_device(self) -> None:
        """Read nameplate to get serial, model, firmware."""
        try:
            nameplate = await asyncio.to_thread(self.controller.read_nameplate)
            if nameplate.get("serial"):
                self.device_info = DeviceInfo(
                    serial=nameplate["serial"],
                    manufacturer=nameplate.get("manufacturer", ""),
                    model=nameplate.get("model", ""),
                    firmware=nameplate.get("version", ""),
                )
                self.status.serial = nameplate["serial"]
                self.status.model = nameplate.get("model")
                self.status.firmware = nameplate.get("version")

                # Persist device info to gateway record
                await update_gateway(
                    self._db,
                    self.gateway_id,
                    serial=self.status.serial,
                    model=self.status.model,
                    firmware=self.status.firmware,
                    last_connected_at=time.time(),
                )
        except Exception as exc:
            logger.warning("Gateway %s: nameplate read failed: %s", self.gateway_id, exc)

    async def _log_startup_state(self) -> None:
        """Log hardware control state at startup."""
        try:
            hw_state = await asyncio.to_thread(self.controller.read_control_status)
            await log_control_event(
                self._db,
                event="startup_hw_snapshot",
                detail=f"wset_ena={hw_state.get('wset_enabled')}, "
                f"wset_pct={hw_state.get('wset_pct')}, "
                f"mode={hw_state.get('loc_rem_ctl_name')}",
                hw_state=hw_state,
                gateway_id=self.gateway_id,
            )
        except Exception as exc:
            logger.warning("Gateway %s: could not read startup hw state: %s", self.gateway_id, exc)

    async def _release_stale_commands(self) -> None:
        """Check for and release stale commands from previous session."""
        prev_state = await load_control_state(self._db, gateway_id=self.gateway_id)
        if not prev_state.get("active"):
            return

        logger.warning(
            "Gateway %s: previous session had active command: %s %dW (started %.0fs ago). "
            "Releasing now for safety.",
            self.gateway_id,
            prev_state["action"],
            prev_state["power_w"],
            time.time() - prev_state["started_at"],
        )
        try:
            await asyncio.to_thread(self.controller.reset_control_state)
            try:
                m704 = self.controller.get_model(704)
                if m704:
                    m704.read()
                    m704.WSetRvrtTms.value = 0
                    m704.WSetEnaRvrt.value = 0
                    m704.write()
            except Exception:
                pass
            await save_control_state(self._db, active=False, gateway_id=self.gateway_id)
            await log_control_event(
                self._db,
                event="startup_release",
                action=prev_state["action"],
                power_w=prev_state["power_w"],
                detail="Released stale command from previous session",
                gateway_id=self.gateway_id,
            )
        except Exception as exc:
            logger.error("Gateway %s: failed to release stale command: %s", self.gateway_id, exc)

    async def _forward_to_global(self, sample: Sample) -> None:
        """Forward per-gateway sample to the global sample bus."""
        await self._global_bus.publish(sample)

    def _get_cached_points(self) -> dict:
        """Return latest poller points (no Modbus call)."""
        s = self.sample_bus.last_sample
        return s.points if s else {}

    async def stop(self) -> None:
        """Gracefully shut down: release commands, stop poller, disconnect."""
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()

        if self.command_handler:
            await self.command_handler.stop()

        if self.poller:
            await self.poller.stop()

        self.status.polling = False
        self.status.connected = False
        self.status.health = "unknown"
        logger.info("Gateway %s stopped", self.gateway_id)

    def to_dict(self) -> dict:
        """Return gateway status for API responses."""
        return {
            "gateway_id": self.gateway_id,
            "name": self.config.name,
            "host": self.config.host,
            "port": self.config.port,
            "unit_id": self.config.unit_id,
            "description": self.config.description,
            "poll_interval": self.config.poll_interval,
            "enabled": self.config.enabled,
            "connected": self.status.connected,
            "polling": self.status.polling,
            "health": self.status.health,
            "serial": self.status.serial,
            "model": self.status.model,
            "firmware": self.status.firmware,
            "ac_type": self.status.ac_type,
            "last_poll_ts": (
                self.poller.state.last_poll_ts if self.poller else None
            ),
            "last_error": self.status.last_error,
        }
