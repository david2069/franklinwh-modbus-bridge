"""FastAPI application with staged lifespan startup."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from franklinwh_bridge import __version__
from franklinwh_bridge.api.admin import router as admin_router
from franklinwh_bridge.api.groups_api import router as groups_router
from franklinwh_bridge.api.health import register_component
from franklinwh_bridge.api.health import router as health_router
from franklinwh_bridge.api.mqtt_api import router as mqtt_router
from franklinwh_bridge.api.ui import router as ui_router
from franklinwh_bridge.config.manager import AppConfig
from franklinwh_bridge.modbus.catalog import capture_catalog, extract_catalog_from_controller
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import Sample, SampleBus
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.backup import BackupManager
from franklinwh_bridge.store.db import (
    get_mqtt_config,
    init_db,
    load_control_state,
    log_control_event,
    log_startup_event,
    save_control_state,
)
from franklinwh_bridge.store.metrics import (
    archive_old_metrics,
    get_retention_days,
    purge_old,
    record_sample,
)
from franklinwh_bridge.store.stats import OperationalStats

logger = logging.getLogger(__name__)

LOG_BUFFER_SIZE = 500


class LogBufferHandler(logging.Handler):
    """In-memory ring buffer for the /api/logs endpoint."""

    def __init__(self, buffer: collections.deque):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(
            {
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": self.format(record),
            }
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = AppConfig()
    config.ensure_dirs()

    log_buffer: collections.deque = collections.deque(maxlen=LOG_BUFFER_SIZE)
    handler = LogBufferHandler(log_buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("franklinwh_bridge").addHandler(handler)
    logging.getLogger("franklinwh_bridge").setLevel(
        getattr(logging, config.settings.log_level.upper(), logging.INFO)
    )

    db = await init_db(config.db_path)
    await log_startup_event(db, "startup", f"v{__version__} env={config.environment}")

    # Ensure the default gateway exists
    import time

    gateway_id = "default"
    async with db.execute("SELECT id FROM gateways WHERE id = ?", (gateway_id,)) as cur:
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO gateways (id, name, host, port, unit_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    gateway_id,
                    "Default Gateway",
                    config.settings.gateway.host,
                    config.settings.gateway.port,
                    config.settings.gateway.unit_id,
                    time.time(),
                ),
            )
            await db.commit()

    stats = await OperationalStats.load(db)
    backup_manager = BackupManager(config.db_path, config.backup_dir)

    sample_bus = SampleBus()

    app.state.config = config
    app.state.db = db
    app.state.stats = stats
    app.state.backup_manager = backup_manager
    app.state.sample_bus = sample_bus
    app.state.gateway_id = gateway_id
    app.state.log_buffer = log_buffer

    mqtt_config = await get_mqtt_config(db)
    env_mqtt = config.settings.mqtt
    mqtt_config["host"] = env_mqtt.host
    mqtt_config["port"] = env_mqtt.port
    if env_mqtt.username:
        mqtt_config["username"] = env_mqtt.username
    if env_mqtt.password:
        mqtt_config["password"] = env_mqtt.password

    mqtt_publisher = MqttPublisher.from_db_config(mqtt_config, gateway_id=gateway_id)
    app.state.mqtt_publisher = mqtt_publisher

    # Sync publishing group filters from DB before starting
    await mqtt_publisher.sync_groups(db)

    if mqtt_config.get("enabled", True):
        await mqtt_publisher.start()

    # Create controller and poller
    poller: ModbusPoller | None = None
    command_handler: CommandHandler | None = None
    controller = None
    purge_task: asyncio.Task | None = None
    try:
        from franklinwh_modbus import FranklinWHController

        gw = config.settings.gateway
        controller = FranklinWHController(ip_address=gw.host, port=gw.port, unit_id=gw.unit_id)

        poller = ModbusPoller(
            controller=controller,
            sample_bus=sample_bus,
            gateway_id=gateway_id,
            poll_interval=gw.poll_interval,
            stats=stats,
        )
        app.state.poller = poller
        app.state.controller = controller

        def _get_cached_points() -> dict:
            """Return latest poller points (no Modbus call)."""
            s = sample_bus.last_sample
            return s.points if s else {}

        command_handler = CommandHandler(
            controller,
            db,
            on_state_changed=mqtt_publisher.publish_command_state,
            points_getter=_get_cached_points,
        )
        mqtt_publisher.set_command_handler(command_handler)
        app.state.command_handler = command_handler

        sample_bus.subscribe(mqtt_publisher.queue_sample)

        # Metrics recorder — writes power readings to the metrics table every sample
        async def _record_metrics(sample: Sample) -> None:
            try:
                written = await record_sample(db, sample.points)
                if written:
                    stats.record_sample_recorded()
                elif any(
                    sample.points.get(k) is not None
                    for k in (
                        "battery_power_w", "grid_power_w",
                        "total_solar", "home_load_ext", "soc",
                    )
                ):
                    # Had power data but was rejected by sanity guard
                    stats.record_sample_rejected()
            except Exception as exc:
                logger.debug("Metrics record failed: %s", exc)
                stats.record_sample_rejected()

        sample_bus.subscribe(_record_metrics)

        # Auto-detect power limits from M702 nameplate on first sample
        _limits_detected = False

        async def _detect_power_limits(sample: Sample) -> None:
            nonlocal _limits_detected
            if _limits_detected:
                return
            charge = sample.points.get("max_charge_rate_w")
            discharge = sample.points.get("max_discharge_rate_w")
            if charge is not None and discharge is not None:
                _limits_detected = True
                charge_w = int(charge)
                discharge_w = int(discharge)
                command_handler.set_power_limits(charge_w, discharge_w)
                mqtt_publisher.set_power_limits(charge_w, discharge_w)
                logger.info(
                    "Power limits detected from M702: charge=%dW, discharge=%dW",
                    charge_w, discharge_w,
                )

        sample_bus.subscribe(_detect_power_limits)

        # Periodic purge + archive of old metrics (runs on startup then hourly)
        async def _metrics_purge_loop() -> None:
            while True:
                try:
                    # Archive raw data > 7 days into 5-min buckets
                    archived = await archive_old_metrics(db)
                    if archived:
                        logger.info("Metrics archive: rolled up %d rows", archived)
                except Exception as exc:
                    logger.warning("Metrics archive failed: %s", exc)

                try:
                    # Purge data older than retention (both raw + archive)
                    retention = await get_retention_days(db)
                    deleted = await purge_old(db, retention)
                    if deleted:
                        logger.info("Metrics purge: removed %d old rows", deleted)
                except Exception as exc:
                    logger.warning("Metrics purge failed: %s", exc)
                await asyncio.sleep(3600)

        purge_task = asyncio.create_task(_metrics_purge_loop())

        async def _init_poller() -> None:
            connected = await asyncio.to_thread(controller.connect)
            if connected:
                nameplate = await asyncio.to_thread(controller.read_nameplate)
                if nameplate.get("serial"):
                    info = DeviceInfo(
                        serial=nameplate["serial"],
                        manufacturer=nameplate.get("manufacturer", ""),
                        model=nameplate.get("model", ""),
                        firmware=nameplate.get("version", ""),
                    )
                    mqtt_publisher.set_device_info(info)
                    logger.info("Device: %s (serial=%s)", info.model, info.serial)

                # Log hardware control state at startup
                try:
                    hw_state = await asyncio.to_thread(controller.read_control_status)
                    await log_control_event(
                        db,
                        event="startup_hw_snapshot",
                        detail=f"wset_ena={hw_state.get('wset_enabled')}, "
                        f"wset_pct={hw_state.get('wset_pct')}, "
                        f"mode={hw_state.get('loc_rem_ctl_name')}",
                        hw_state=hw_state,
                    )
                    logger.info(
                        "Startup HW state: WSetEna=%s, WSetPct=%s, LocRemCtl=%s",
                        hw_state.get("wset_enabled"),
                        hw_state.get("wset_pct"),
                        hw_state.get("loc_rem_ctl_name"),
                    )
                except Exception as exc:
                    logger.warning("Could not read startup hw state: %s", exc)

                # Check if we had an active command before last shutdown
                prev_state = await load_control_state(db)
                if prev_state.get("active"):
                    logger.warning(
                        "Previous session had active command: %s %dW (started %.0fs ago). "
                        "Releasing now for safety.",
                        prev_state["action"],
                        prev_state["power_w"],
                        time.time() - prev_state["started_at"],
                    )
                    try:
                        await asyncio.to_thread(controller.reset_control_state)
                        # Also clear the revert timer to exit VPP mode
                        try:
                            m704 = controller.get_model(704)
                            if m704:
                                m704.read()
                                m704.WSetRvrtTms.value = 0
                                m704.WSetEnaRvrt.value = 0
                                m704.write()
                        except Exception:
                            pass
                        # Clear the persisted state so we don't release again
                        await save_control_state(db, active=False)
                        await log_control_event(
                            db,
                            event="startup_release",
                            action=prev_state["action"],
                            power_w=prev_state["power_w"],
                            detail="Released stale command from previous session",
                        )
                    except Exception as exc:
                        logger.error("Failed to release stale command: %s", exc)

                # Capture SunSpec catalog from model definitions (no extra I/O)
                try:
                    device_info = await asyncio.to_thread(
                        extract_catalog_from_controller, controller
                    )
                    cat_hash, cat_diff = await capture_catalog(
                        device_info, db, gateway_id
                    )
                    logger.info(
                        "SunSpec catalog captured: %d models (hash=%s)",
                        len(device_info.get("models", {})),
                        cat_hash,
                    )
                except Exception as exc:
                    logger.warning("Catalog capture failed: %s", exc)

                # Wire reader_fn for POST /api/models/refresh
                async def _reader_fn():
                    """Re-extract catalog from controller's in-memory model defs."""
                    try:
                        info = await asyncio.to_thread(
                            extract_catalog_from_controller, controller
                        )
                        return info, None
                    except Exception as exc:
                        return None, str(exc)

                app.state.reader_fn = _reader_fn

                # Detect AC wiring type while controller is connected
                try:
                    await asyncio.to_thread(poller._detect_ac_type)
                    mqtt_publisher.set_ac_type(poller.ac_type)
                except Exception as exc:
                    logger.warning("AC type detection failed: %s", exc)

                await asyncio.to_thread(controller.disconnect)

            await poller.start()

        asyncio.create_task(_init_poller())

    except Exception as exc:
        logger.warning("Poller init failed (no hardware?): %s", exc)

    register_component(
        "poller",
        lambda: (
            {
                "status": "running" if poller and poller.state.connected else "disconnected",
                "polls_total": poller.state.polls_total if poller else 0,
                "last_poll_ts": poller.state.last_poll_ts if poller else None,
                "last_error": poller.state.last_error if poller else None,
            }
            if poller
            else {"status": "not_configured"}
        ),
    )
    register_component(
        "mqtt",
        lambda: {
            "connected": mqtt_publisher.state.connected,
            "messages_sent": mqtt_publisher.state.messages_sent,
            "discovery_published": mqtt_publisher.state.discovery_published,
        },
    )
    register_component("stats", lambda: stats.snapshot.to_dict())

    logger.info("Bridge started (env=%s, v%s)", config.environment, __version__)

    yield

    logger.info("Bridge shutting down — releasing control and logging state")

    # 1. Stop command handler first (releases active battery commands)
    if command_handler:
        await command_handler.stop()

    # 2. Take final hw state snapshot before disconnecting
    if controller:
        try:
            hw_state = await asyncio.to_thread(controller.read_control_status)
            await log_control_event(
                db,
                event="shutdown_hw_snapshot",
                detail=f"wset_ena={hw_state.get('wset_enabled')}, "
                f"wset_pct={hw_state.get('wset_pct')}",
                hw_state=hw_state,
            )
            logger.info(
                "Shutdown HW state: WSetEna=%s, WSetPct=%s",
                hw_state.get("wset_enabled"),
                hw_state.get("wset_pct"),
            )
        except Exception as exc:
            logger.warning("Could not read shutdown hw state: %s", exc)

    # 3. Cancel metrics purge task
    if purge_task and not purge_task.done():
        purge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purge_task

    # 4. Stop poller (graceful Modbus disconnect)
    if poller:
        await poller.stop()

    # 5. Stop MQTT
    await mqtt_publisher.stop()

    # 6. Flush operational stats one final time
    await stats.flush()

    await log_startup_event(db, "shutdown", f"v{__version__}")
    await db.close()
    logger.info("Bridge shutdown complete")


app = FastAPI(
    title="franklinwh-modbus-bridge",
    version=__version__,
    lifespan=lifespan,
)

# Mount static files
_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# API routers (before UI catch-all)
app.include_router(health_router)
app.include_router(admin_router)
app.include_router(mqtt_router)
app.include_router(groups_router)

# UI router (serves GET / and POST /api/command)
app.include_router(ui_router)
