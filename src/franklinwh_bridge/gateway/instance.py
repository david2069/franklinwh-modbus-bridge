"""GatewayInstance — encapsulates everything for one aGate connection.

Each instance owns a controller, poller, command handler, modbus lock,
and per-gateway sample bus.  The instance lifecycle mirrors what main.py's
_init_poller() does for the single "default" gateway, but is reusable
for any number of gateways.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from franklinwh_bridge.gateway import link_stats
from franklinwh_bridge.gateway.battery_rating import BatteryRating
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


def disable_connect_writes(controller: Any) -> None:
    """Stop the library writing to the aGate when it connects (#239).

    franklinwh-modbus <= 0.9.5 runs ``_test_extension_writability()`` inside
    ``connect()``: it writes test values to the operating mode (15507, even
    Emergency Backup) and both reserves (15508/15509), then writes the
    originals back. The bridge connects at every start and reconnect, so a live
    aGate had its mode and reserves rewritten each time, and left wrong if the
    restore failed. Nothing here uses the result. The library dropped the test
    on its develop branch (its #24); keep this until the pin is past it (#230).
    """
    if hasattr(controller, "_test_extension_writability"):
        controller._test_extension_writability = lambda: {}


def _close_device_socket(controller: Any) -> None:
    dev = getattr(controller, "dev", None)
    if dev is not None:
        with contextlib.suppress(Exception):
            dev.disconnect()  # SunSpecModbusClientDeviceTCP -> ModbusClientTCP.disconnect


def close_sockets_on_disconnect(controller: Any) -> None:
    """Make the library actually close its TCP socket (#64, #288).

    franklinwh-modbus ``disconnect()`` calls ``dev.close()``, which for
    pysunspec2's ``SunSpecModbusClientDeviceTCP`` (1.3.6) is the base class's
    no-op, then drops ``dev``: the socket stays open. ``connect()`` and
    ``reconnect()`` replace ``dev`` the same way. Every disconnect or reconnect
    left a live session on the aGate, which serves one client at a time: the
    CLOSE_WAIT socket of #64, and the reason a second connection (the Explorer
    reader, the library's per-poll extension reads) was refused on a strict
    one-client device. Wrap both so the old socket is closed first.
    """
    for name in ("disconnect", "connect", "reconnect"):
        original = getattr(controller, name, None)
        if original is None or getattr(original, "_closes_socket", False):
            continue

        def wrapped(*args, _original=original, **kwargs):
            _close_device_socket(controller)
            return _original(*args, **kwargs)

        wrapped._closes_socket = True  # type: ignore[attr-defined]
        setattr(controller, name, wrapped)


#: The order SunSpec bases are probed in: the aGate's own base first (#308).
SUNSPEC_BASE_ORDER = (0, 40000, 50000)


def prefer_sunspec_base_0() -> None:
    """Make the library's SunSpec scan bind base 0 before the 40000 mirror (#308).

    franklinwh-modbus asks for base 0, but pysunspec2 1.3.6's ``scan()`` takes
    no base: it probes ``base_addr_list = [40000, 0, 50000]`` in order. aGate
    firmware up to V10R01B04D00 refused 40000, so the scan fell back to 0;
    V12R02B60D00 mirrors the whole map at 40000, so the scan would bind there,
    sending every model read and write to the mirror and shifting the catalog's
    addresses by 40000. Swap in a device class that probes 0 first.
    """
    from franklinwh_modbus import controller as lib

    base = getattr(lib, "SunSpecModbusClientDeviceTCP", None)
    if base is None or getattr(base, "_base_0_first", False):
        return

    class Base0FirstDevice(base):  # type: ignore[misc, valid-type]
        _base_0_first = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            rest = [a for a in getattr(self, "base_addr_list", []) if a not in SUNSPEC_BASE_ORDER]
            self.base_addr_list = [*SUNSPEC_BASE_ORDER, *rest]

    lib.SunSpecModbusClientDeviceTCP = Base0FirstDevice


@dataclass
class GatewayConfig:
    """Configuration for a single gateway."""

    gateway_id: str
    name: str
    host: str
    port: int = 502
    unit_id: int = 1
    poll_interval: int = 10
    timeout: float = 10.0
    description: str = ""
    enabled: bool = True
    mock: bool = False  # synthetic gateway — no Modbus connection
    service_id: str | None = None  # utility-service link (SCH3 fan-out target)
    # 'agate' (full battery system) or 'mac1' (Meter Adaptor Collar: metering
    # only — no battery, far fewer SunSpec models, so absent models are normal).
    device_type: str = "agate"
    # 0 single phase, 1 split phase (L1+L2), 2 three phase. A real gateway
    # overwrites this by detecting from Modbus; a mock has nothing to detect
    # and uses it as-is, which is what lets a mock stand in for a US aGate.
    ac_type: int = 0
    # Home load register: 'standard' (15506, 100 W steps) or 'high_res'
    # (undocumented 16000, ~1 W, sanity-checked against 15506). #35.
    home_load_source: str = "standard"


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
        if self.config.mock:
            await self._start_mock()
            return

        try:
            from franklinwh_modbus import FranklinWHController
        except ImportError:
            logger.error(
                "Gateway %s: franklinwh-modbus not installed", self.gateway_id
            )
            return

        cfg = self.config
        # Link-health measurement (Phase 0b): count library connects and
        # log-reported retries/rescans, attributed to this gateway.
        link_stats.install()
        link_stats.register_address(self.gateway_id, cfg.host, cfg.port)
        prefer_sunspec_base_0()
        self.controller = FranklinWHController(
            ip_address=cfg.host, port=cfg.port, unit_id=cfg.unit_id,
            timeout=cfg.timeout,
        )
        disable_connect_writes(self.controller)
        close_sockets_on_disconnect(self.controller)

        self.poller = ModbusPoller(
            controller=self.controller,
            sample_bus=self.sample_bus,
            gateway_id=self.gateway_id,
            poll_interval=cfg.poll_interval,
            stats=self._stats,
            modbus_lock=self.modbus_lock,
            timeout=cfg.timeout,
            home_load_source=lambda: self.config.home_load_source,
        )

        # Fan-in: forward per-gateway samples to the global bus
        self.sample_bus.subscribe(self._forward_to_global)

        rating = BatteryRating(self.gateway_id)
        await rating.load(self._db)
        self.command_handler = CommandHandler(
            self.controller,
            self._db,
            points_getter=self._get_cached_points,
            modbus_lock=self.modbus_lock,
            gateway_id=self.gateway_id,
            rating=rating,
        )
        # Lets the handler notice a dispatch it did not send (another tool).
        self.sample_bus.subscribe(self.command_handler.observe_sample)

        # Run the connect + discover sequence in background
        self._init_task = asyncio.create_task(self._init_and_poll())

    async def _start_mock(self) -> None:
        """Start a synthetic gateway: emit simulated samples, no Modbus.

        No connection is opened, so a mock never contends for a real aGate's
        session, and it gets a synthetic ``MOCK-<id>`` serial that can't clash
        in HA Discovery.

        Control IS wired, to simulated state: a real ``CommandHandler`` drives
        the ``MockController``, so mode/reserve/battery commands — from HA, the
        Controls tab or a schedule — take the same path as on a real aGate and
        the synthetic samples follow them.
        """
        from franklinwh_bridge.gateway.mock_gateway import MockController, MockPoller

        # A real gateway detects its AC type from Modbus; a mock has nothing to
        # detect, so it honours whatever the gateway record declares. That makes
        # a mock a usable stand-in for a US split-phase (L1+L2) aGate — the
        # topology the FranklinWH Cloud and Local APIs assume unconditionally —
        # without US hardware.
        ac_type = int(self.config.ac_type or 0)
        self.controller = MockController(self.gateway_id, ac_type=ac_type)
        serial = self.controller.serial
        self.device_info = DeviceInfo(
            serial=serial,
            manufacturer="FranklinWH (mock)",
            model="aGate (mock)",
            firmware="MOCK",
            gateway_id=self.gateway_id,
            # Name it in HA too, not just in the model/serial fields. A device
            # card shows its NAME; "FHP 2" beside a real gateway invites someone
            # to wire an automation to synthetic data.
            name=f"{self.config.name or self.gateway_id} (mock)",
        )
        self.status.connected = True
        self.status.health = "connected"
        self.status.serial = serial
        self.status.model = "aGate (mock)"
        self.status.firmware = "MOCK"
        self.status.ac_type = ac_type

        self.poller = MockPoller(
            self.sample_bus, self.gateway_id, self.config.poll_interval,
            ac_type=ac_type, controller=self.controller,
        )
        rating = BatteryRating(self.gateway_id, mock=True)
        await rating.load(self._db)
        self.command_handler = CommandHandler(
            self.controller,
            self._db,
            points_getter=self._get_cached_points,
            modbus_lock=self.modbus_lock,
            gateway_id=self.gateway_id,
            rating=rating,
        )
        # Fan-in to the global bus so the Site aggregator picks up the mock.
        self.sample_bus.subscribe(self._forward_to_global)
        self.sample_bus.subscribe(self.command_handler.observe_sample)
        await self.poller.start()
        self.status.polling = True

        await update_gateway(
            self._db,
            self.gateway_id,
            serial=serial,
            model="aGate (mock)",
            firmware="MOCK",
            last_connected_at=time.time(),
        )
        logger.info(
            "Gateway %s started (MOCK): %s — synthetic data, no hardware",
            self.gateway_id, self.config.name,
        )

    async def _init_and_poll(self) -> None:
        """Connect, discover device info, release stale commands, start polling."""
        try:
            connected = await asyncio.to_thread(
                self._tracked, self.controller.connect, "connect", "startup",
            )
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
                    self._tracked,
                    lambda: extract_catalog_from_controller(self.controller),
                    "model_read", "catalog",
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
                        self._tracked,
                        lambda: extract_catalog_from_controller(ctrl),
                        "model_read", "catalog",
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

    def _tracked(self, fn: Any, kind: str, path: str, check: bool = True) -> Any:
        """Run one blocking aGate request under link-health measurement."""
        with link_stats.track(self.gateway_id, kind, path) as req:
            result = fn()
            if check:
                req.check(result)
            return result

    async def _discover_device(self) -> None:
        """Read nameplate to get serial, model, firmware."""
        try:
            nameplate = await asyncio.to_thread(
                self._tracked, self.controller.read_nameplate, "model_read", "startup",
            )
            if nameplate.get("serial"):
                self.device_info = DeviceInfo(
                    serial=nameplate["serial"],
                    manufacturer=nameplate.get("manufacturer", ""),
                    model=nameplate.get("model", ""),
                    firmware=nameplate.get("version", ""),
                    gateway_id=self.gateway_id,
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
            hw_state = await asyncio.to_thread(
                self._tracked, self.controller.read_control_status, "model_read", "startup",
            )
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
            await asyncio.to_thread(
                self._tracked, self.controller.reset_control_state, "write", "startup", False,
            )
            try:
                m704 = self.controller.get_model(704)
                if m704:
                    with link_stats.track(self.gateway_id, "write", "startup"):
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

    def latest_points(self) -> dict:
        """Public: latest cached poller points for this gateway (no Modbus call).

        Used by the schedule engine to build the sensor snapshot that entry/exit
        condition trees evaluate against, without adding Modbus traffic.
        """
        return self._get_cached_points()

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
