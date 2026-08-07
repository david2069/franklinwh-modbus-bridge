"""FastAPI application with staged lifespan startup."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from franklinwh_bridge import __version__
from franklinwh_bridge.api.admin import router as admin_router
from franklinwh_bridge.api.gateways_api import router as gateways_router
from franklinwh_bridge.api.groups_api import router as groups_router
from franklinwh_bridge.api.health import register_component
from franklinwh_bridge.api.health import router as health_router
from franklinwh_bridge.api.mqtt_api import router as mqtt_router
from franklinwh_bridge.api.schedules_api import router as schedules_router
from franklinwh_bridge.api.ui import router as ui_router
from franklinwh_bridge.config.manager import AppConfig
from franklinwh_bridge.gateway.aggregator import SiteAggregator
from franklinwh_bridge.gateway.health import HealthChecker
from franklinwh_bridge.gateway.registry import GatewayRegistry
from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.modbus.sample import Sample, SampleBus
from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher
from franklinwh_bridge.store.alarms import AlarmTracker
from franklinwh_bridge.store.backup import BackupManager
from franklinwh_bridge.store.db import (
    get_gateway,
    get_gateways,
    get_mqtt_config,
    init_db,
    log_schedule_event,
    log_startup_event,
)
from franklinwh_bridge.store.metrics import (
    archive_old_metrics,
    get_raw_age_days,
    get_retention_days,
    log_metrics_snapshot,
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
        # Extract gateway_id from record extra dict (set by LoggerAdapter)
        # or infer from message pattern "Gateway {id}:"
        gw_id = getattr(record, "gateway_id", "")
        if not gw_id:
            msg = self.format(record)
            if msg.startswith("Gateway ") and ":" in msg:
                gw_id = msg.split(":")[0].replace("Gateway ", "").strip()
        else:
            msg = self.format(record)
        self._buffer.append(
            {
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": msg,
                "gateway_id": gw_id,
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

    # Ensure the default gateway exists in the DB
    gateway_id = "default"
    async with db.execute(
        "SELECT id FROM gateways WHERE id = ?", (gateway_id,)
    ) as cur:
        if not await cur.fetchone():
            gw = config.settings.gateway
            await db.execute(
                "INSERT INTO gateways "
                "(id, name, host, port, unit_id, enabled, created_at, "
                " poll_interval, description) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    gateway_id,
                    "Default Gateway",
                    gw.host,
                    gw.port,
                    gw.unit_id,
                    time.time(),
                    gw.poll_interval,
                    "Auto-created from environment config",
                ),
            )
            await db.commit()

    stats = await OperationalStats.load(db)
    backup_manager = BackupManager(config.db_path, config.backup_dir)

    # Global sample bus — receives fan-in from all per-gateway buses
    sample_bus = SampleBus()

    app.state.config = config
    app.state.db = db
    app.state.stats = stats
    app.state.backup_manager = backup_manager
    app.state.sample_bus = sample_bus
    app.state.log_buffer = log_buffer

    # ── MQTT Publisher ────────────────────────────────────────
    mqtt_config = await get_mqtt_config(db)
    env_mqtt = config.settings.mqtt
    mqtt_config["host"] = env_mqtt.host
    mqtt_config["port"] = env_mqtt.port
    if env_mqtt.username:
        mqtt_config["username"] = env_mqtt.username
    if env_mqtt.password:
        mqtt_config["password"] = env_mqtt.password

    mqtt_publisher = MqttPublisher.from_db_config(
        mqtt_config, gateway_id=gateway_id,
    )
    app.state.mqtt_publisher = mqtt_publisher

    await mqtt_publisher.sync_groups(db)

    if mqtt_config.get("enabled", True):
        await mqtt_publisher.start()

    # Subscribe MQTT publisher to the global sample bus (receives all gateways)
    sample_bus.subscribe(mqtt_publisher.queue_sample)

    # Site aggregator — computes virtual site-level metrics from all gateways
    site_aggregator = SiteAggregator()
    sample_bus.subscribe(site_aggregator.on_sample)
    app.state.site_aggregator = site_aggregator

    # ── Gateway Registry ──────────────────────────────────────
    registry = GatewayRegistry(db=db, global_bus=sample_bus, stats=stats)
    app.state.registry = registry

    # Backward-compatible accessors for the default gateway.
    # API routes that haven't been refactored yet use these.
    app.state.gateway_id = gateway_id

    # ── Global Sample Bus Subscribers ─────────────────────────

    # Per-gateway grid_mode tracking for change-event logging
    _last_grid_mode: dict[str, str | None] = {}

    # Alarm tracker — writes alarm_events rows on state changes
    alarm_tracker = AlarmTracker(db)

    # Metrics recorder — writes power readings to the metrics table
    async def _record_metrics(sample: Sample) -> None:
        # Mock gateways emit synthetic data — never persist it, so it can't
        # pollute real gateways' Power History or storage.
        inst = registry.get(sample.gateway_id)
        if inst is not None and getattr(inst.config, "mock", False):
            return
        try:
            written = await record_sample(
                db, sample.points, gateway_id=sample.gateway_id,
            )
            if written:
                stats.record_sample_recorded()
            elif any(
                sample.points.get(k) is not None
                for k in (
                    "battery_power_w", "grid_power_w",
                    "total_solar", "home_load_ext", "soc",
                )
            ):
                stats.record_sample_rejected()
        except Exception as exc:
            logger.debug("Metrics record failed: %s", exc)
            stats.record_sample_rejected()

        # Log grid_mode state changes as discrete events
        gw_id = sample.gateway_id
        new_mode = sample.points.get("grid_mode")
        prev_mode = _last_grid_mode.get(gw_id, "UNSET")
        if new_mode is not None and new_mode != prev_mode:
            _last_grid_mode[gw_id] = new_mode
            if prev_mode != "UNSET":
                detail = f"{prev_mode} → {new_mode} (gateway={gw_id})"
                logger.info("Grid mode changed: %s", detail)
                with contextlib.suppress(Exception):
                    await log_startup_event(db, "grid_mode_change", detail)

        # Alarm change detection — writes alarm_events rows on register transitions
        inst = registry.get(sample.gateway_id)
        if inst is None or not getattr(inst.config, "mock", False):
            with contextlib.suppress(Exception):
                await alarm_tracker.process_sample(sample.points, sample.gateway_id)

    sample_bus.subscribe(_record_metrics)

    # Auto-detect power limits from M702 nameplate on first sample
    _limits_detected: dict[str, bool] = {}

    async def _detect_power_limits(sample: Sample) -> None:
        gw_id = sample.gateway_id
        if _limits_detected.get(gw_id):
            return
        charge = sample.points.get("max_charge_rate_w")
        discharge = sample.points.get("max_discharge_rate_w")
        if charge is not None and discharge is not None:
            _limits_detected[gw_id] = True
            charge_w = int(charge)
            discharge_w = int(discharge)
            inst = registry.get(gw_id)
            if inst and inst.command_handler:
                inst.command_handler.set_power_limits(charge_w, discharge_w)
            # Update MQTT publisher limits (for default gateway)
            if gw_id == "default":
                mqtt_publisher.set_power_limits(charge_w, discharge_w)
            logger.info(
                "Gateway %s: power limits detected — charge=%dW, discharge=%dW",
                gw_id, charge_w, discharge_w,
            )

    sample_bus.subscribe(_detect_power_limits)

    # ── Metrics Purge Loop ────────────────────────────────────
    purge_task: asyncio.Task | None = None

    async def _metrics_purge_loop() -> None:
        while True:
            # archive_old_metrics / purge_old log their own one-line summary
            # when they actually move rows; don't double-log it here.
            try:
                raw_age_days = await get_raw_age_days(db)
                await archive_old_metrics(db, raw_age_s=raw_age_days * 86400)
            except Exception as exc:
                logger.warning("Metrics archive failed: %s", exc)

            try:
                retention = await get_retention_days(db)
                await purge_old(db, retention)
            except Exception as exc:
                logger.warning("Metrics purge failed: %s", exc)
            await asyncio.sleep(3600)

    purge_task = asyncio.create_task(_metrics_purge_loop())

    # ── Schedule Engine (SCH1) ────────────────────────────────
    # Resolver maps a schedule target to live (gateway_id, command-handler)
    # pairs. 'gateway' → one gateway; 'site' → every running gateway; 'service'
    # → every running gateway linked to that service (SCH3 fan-out). Member
    # gateways are read from each running instance's config.service_id, kept in
    # sync by the gateways PATCH endpoint.
    def _schedule_resolver(
        target_type: str, target_id: str | None,
    ) -> list[tuple[str, object]]:
        pairs: list[tuple[str, object]] = []
        if target_type == "gateway":
            gw_id = target_id or "default"
            inst = registry.get(gw_id)
            if inst and inst.command_handler:
                pairs.append((gw_id, inst.command_handler))
        elif target_type == "site":
            for gw_id in registry.list_active():
                inst = registry.get(gw_id)
                if inst and inst.command_handler:
                    pairs.append((gw_id, inst.command_handler))
        elif target_type == "service" and target_id:
            for gw_id in registry.list_active():
                inst = registry.get(gw_id)
                if (
                    inst and inst.command_handler
                    and getattr(inst.config, "service_id", None) == target_id
                ):
                    pairs.append((gw_id, inst.command_handler))
        return pairs

    async def _schedule_audit(
        schedule_id: str | None, action: str, target: str,
        result: str, detail: str,
    ) -> None:
        await log_schedule_event(db, schedule_id, action, target, result, detail)

    def _schedule_points(gw_id: str) -> dict:
        """Latest cached points for a gateway (no Modbus call) — feeds the sensor
        snapshot the engine evaluates entry/exit condition trees against."""
        inst = registry.get(gw_id)
        return inst.latest_points() if inst else {}

    schedule_engine = ScheduleEngine(
        db, _schedule_resolver, on_audit=_schedule_audit,
        points_fn=_schedule_points,
    )
    app.state.schedule_engine = schedule_engine

    # ── Health Checker ─────────────────────────────────────────
    health_checker = HealthChecker(registry)
    app.state.health_checker = health_checker

    # ── Start All Gateways ────────────────────────────────────

    async def _bring_up_gateways() -> None:
        """Start enabled gateways + wire the default's MQTT/command handler.

        Isolated from the health-checker/engine start so a flaky gateway
        connection (e.g. the aGate's single Modbus session not yet freed after
        a fast restart) can't abort the whole startup. Errors are logged; the
        gateway's own reconnect loop recovers it.
        """
        await registry.start_all()

        # Feed each gateway's assigned phase to the site aggregator so it can
        # bucket per-phase site totals (Topology A).
        for gw in await get_gateways(db):
            site_aggregator.set_gateway_phase(gw["id"], gw.get("phase", "all"))

        # Wire the default gateway's command handler + device info to MQTT
        default = registry.get("default")
        if default:
            if default.command_handler:
                default.command_handler._on_state_changed = (
                    mqtt_publisher.publish_command_state
                )
                mqtt_publisher.set_command_handler(default.command_handler)

            # Wait briefly for the init task to discover device info
            for _ in range(20):
                if default.device_info:
                    mqtt_publisher.set_device_info(default.device_info)
                    mqtt_publisher.set_ac_type(default.status.ac_type)
                    gw_row = await get_gateway(db, "default")
                    if gw_row:
                        mqtt_publisher.set_phase_view(gw_row.get("phase_view", "both"))
                    break
                await asyncio.sleep(0.5)

            # Wire reader_fn for POST /api/models/refresh
            if default.reader_fn:
                app.state.reader_fn = default.reader_fn

    async def _start_gateways() -> None:
        """Bring up gateways, then ALWAYS start the health checker + schedule
        engine — even if gateway bring-up failed (they no-op until a gateway is
        available, and must survive a flaky/contended start)."""
        try:
            await _bring_up_gateways()
        except Exception as exc:
            logger.error("Gateway bring-up failed: %s", exc, exc_info=True)

        try:
            await health_checker.start()
        except Exception as exc:
            logger.error("Health checker start failed: %s", exc)

        try:
            # The schedule engine no-ops on targets with no running handler, so
            # a late-arriving or failed gateway is fine.
            await schedule_engine.start()
        except Exception as exc:
            logger.error("Schedule engine start failed: %s", exc)

        try:
            await log_metrics_snapshot(db, "metrics_snapshot_startup")
        except Exception as exc:
            logger.warning("Metrics snapshot (startup) failed: %s", exc)

    asyncio.create_task(_start_gateways())

    # ── Health Components ─────────────────────────────────────
    register_component(
        "poller",
        lambda: _poller_health(registry),
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
    register_component(
        "gateways",
        lambda: {
            "count": len(registry.list_all()),
            "active": len(registry.list_active()),
            "gateways": registry.status_all(),
        },
    )

    logger.info("Bridge started (env=%s, v%s)", config.environment, __version__)

    yield

    # ── Shutdown ──────────────────────────────────────────────
    logger.info("Bridge shutting down — releasing control and logging state")

    # 1. Stop the schedule engine (so it can't re-dispatch during teardown),
    #    then the health checker.
    await schedule_engine.stop()
    await health_checker.stop()

    # 2. Stop all gateways (releases commands, stops pollers, disconnects)
    await registry.stop_all()

    # 2. Cancel metrics purge task
    if purge_task and not purge_task.done():
        purge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purge_task

    # 3. Stop MQTT
    await mqtt_publisher.stop()

    # 4. Flush operational stats
    await stats.flush()

    try:
        await log_metrics_snapshot(db, "metrics_snapshot_shutdown")
    except Exception as exc:
        logger.warning("Metrics snapshot (shutdown) failed: %s", exc)

    await log_startup_event(db, "shutdown", f"v{__version__}")
    await db.close()
    logger.info("Bridge shutdown complete")


def _poller_health(registry: GatewayRegistry) -> dict:
    """Build health data for the poller component."""
    default = registry.get("default")
    if default and default.poller:
        p = default.poller
        return {
            "status": "running" if p.state.connected else "disconnected",
            "polls_total": p.state.polls_total,
            "last_poll_ts": p.state.last_poll_ts,
            "last_error": p.state.last_error,
        }
    return {"status": "not_configured"}


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
app.include_router(gateways_router)
app.include_router(schedules_router)

# UI router (serves GET / and POST /api/command)
app.include_router(ui_router)
