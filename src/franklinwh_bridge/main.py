"""FastAPI application with staged lifespan startup."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge import __version__, disclaimer, workers
from franklinwh_bridge.alert_delivery import AlertDelivery
from franklinwh_bridge.alert_monitor import AlertMonitor
from franklinwh_bridge.api.admin import router as admin_router
from franklinwh_bridge.api.alerts_api import router as alerts_router
from franklinwh_bridge.api.auth import require_admin_for_writes, require_auth
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.diagnostics_api import router as diagnostics_router
from franklinwh_bridge.api.disclaimer_api import router as disclaimer_router
from franklinwh_bridge.api.energy_api import router as energy_router
from franklinwh_bridge.api.gateways_api import router as gateways_router
from franklinwh_bridge.api.groups_api import router as groups_router
from franklinwh_bridge.api.ha_api import router as ha_router
from franklinwh_bridge.api.health import register_component
from franklinwh_bridge.api.health import router as health_router
from franklinwh_bridge.api.modules_api import require_module
from franklinwh_bridge.api.modules_api import router as modules_router
from franklinwh_bridge.api.mqtt_api import router as mqtt_router
from franklinwh_bridge.api.nem_api import router as nem_router
from franklinwh_bridge.api.point_history_api import router as point_history_router
from franklinwh_bridge.api.reader_api import router as reader_router
from franklinwh_bridge.api.scheduler_api import router as scheduler_router
from franklinwh_bridge.api.schedules_api import router as schedules_router
from franklinwh_bridge.api.setup_api import router as setup_router
from franklinwh_bridge.api.solar_api import router as solar_router
from franklinwh_bridge.api.support_api import router as support_router
from franklinwh_bridge.api.tariff_api import router as tariff_router
from franklinwh_bridge.api.ui import router as ui_router
from franklinwh_bridge.api.users_api import router as users_router
from franklinwh_bridge.config.clock import check_and_record
from franklinwh_bridge.config.manager import AppConfig
from franklinwh_bridge.config.mqtt_broker import resolve_broker
from franklinwh_bridge.config.supervisor import (
    MQTT_WATCH_INTERVAL_S,
    apply_timezone,
    discover_mqtt,
    ensure_local_ha_instance,
    supervisor_token,
)
from franklinwh_bridge.gateway.aemo_nem import NemPricingService
from franklinwh_bridge.gateway.aggregator import SiteAggregator
from franklinwh_bridge.gateway.billing import BillingStore
from franklinwh_bridge.gateway.connectivity import ConnectivityMonitor
from franklinwh_bridge.gateway.constants import ConstantsStore
from franklinwh_bridge.gateway.demand import DemandTracker
from franklinwh_bridge.gateway.energy_totals import EnergyTotals
from franklinwh_bridge.gateway.fixed_charges import FixedChargesStore
from franklinwh_bridge.gateway.ha import HaRegistry
from franklinwh_bridge.gateway.health import HealthChecker
from franklinwh_bridge.gateway.registry import GatewayRegistry
from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.gateway.solar_forecast import SolarForecastService
from franklinwh_bridge.modbus.sample import Sample, SampleBus
from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher
from franklinwh_bridge.publish.namespace import (
    choose_initial_namespace,
    ensure_client_id,
    mark_published,
)
from franklinwh_bridge.security import seed_admin, session_secret_key
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store import link_stats as link_stats_store
from franklinwh_bridge.store import modbus_audit, point_history
from franklinwh_bridge.store.alarms import AlarmTracker
from franklinwh_bridge.store.backup import BackupManager
from franklinwh_bridge.store.db import (
    UNCONFIGURED_DESCRIPTION,
    get_app_config,
    get_gateway,
    get_gateways,
    get_mqtt_config,
    init_db,
    insert_logs,
    log_schedule_event,
    log_startup_event,
    purge_logs,
    set_outage_catchup,
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
from franklinwh_bridge.support.bundle import ensure_install_date

logger = logging.getLogger(__name__)

LOG_BUFFER_SIZE = 500
LOG_PERSIST_QUEUE_MAX = 10000  # pending log rows awaiting a flush to SQLite
LOG_FLUSH_INTERVAL_S = 5
LOG_RETENTION_DAYS = 30  # persisted logs older than this are purged


class LogBufferHandler(logging.Handler):
    """In-memory ring buffer for the recent-logs view, plus an optional persist
    queue (drained to SQLite by a background flusher) so logs survive restarts
    and can be filtered by time span."""

    def __init__(
        self,
        buffer: collections.deque,
        persist_queue: collections.deque | None = None,
        persist_level: int = logging.INFO,
    ):
        super().__init__()
        self._buffer = buffer
        self._persist = persist_queue
        self._persist_level = persist_level

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
        entry = {
            "ts": record.created,
            "level": record.levelname,
            "name": record.name,
            "message": msg,
            "gateway_id": gw_id,
        }
        self._buffer.append(entry)
        # Persist INFO+ (deque append is thread-safe; the flusher drains it).
        if self._persist is not None and record.levelno >= self._persist_level:
            self._persist.append(entry)


#: One format for every line on the console, the bridge's and uvicorn's alike.
#: uvicorn's default has no time at all, so an add-on/Docker log couldn't say
#: when anything happened.
CONSOLE_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _QuietAccessLog(logging.Filter):
    """Drop successful GETs from uvicorn's access log unless at DEBUG.

    The UI polls several endpoints every few seconds and the container health
    check hits /api/status, so at INFO the access log was nothing but
    ``GET ... 200 OK`` — burying the lines that matter. Writes (POST, PATCH,
    DELETE) and every error response are kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if logging.getLogger("franklinwh_bridge").isEnabledFor(logging.DEBUG):
            return True
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 5:
            method, status = args[1], args[4]
            if method == "GET" and isinstance(status, int) and status < 400:
                return False
        return True


#: franklinwh-modbus logs under this name; WARNING and above reach the bridge's logs.
LIBRARY_LOGGER = "franklinwh_modbus"


def configure_console_logging(level: int) -> None:
    """Send the bridge's own log to stderr, timestamped, and timestamp uvicorn's.

    Until this, ``franklinwh_bridge`` logged only to the in-app Logs tab, so
    Home Assistant's add-on Log and ``docker logs`` showed uvicorn alone —
    no startup version, no warnings, nothing the bridge itself said.
    Idempotent: the app is booted many times in one test process.
    """
    fmt = logging.Formatter(CONSOLE_LOG_FORMAT, CONSOLE_DATE_FORMAT)
    bridge = logging.getLogger("franklinwh_bridge")
    if not any(getattr(h, "_bridge_console", False) for h in bridge.handlers):
        console = logging.StreamHandler(sys.stderr)
        console._bridge_console = True  # type: ignore[attr-defined]
        console.setFormatter(fmt)
        bridge.addHandler(console)
    bridge.setLevel(level)
    # The library's own warnings and errors, e.g. a failed internal reconnect (#318).
    library = logging.getLogger(LIBRARY_LOGGER)
    if not any(getattr(h, "_bridge_console", False) for h in library.handlers):
        lib_console = logging.StreamHandler(sys.stderr)
        lib_console._bridge_console = True  # type: ignore[attr-defined]
        lib_console.setFormatter(fmt)
        library.addHandler(lib_console)
    library.setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.setFormatter(fmt)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietAccessLog) for f in access.filters):
        access.addFilter(_QuietAccessLog())


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = AppConfig()
    config.ensure_dirs()

    log_buffer: collections.deque = collections.deque(maxlen=LOG_BUFFER_SIZE)
    # Persist queue drained to SQLite by _log_flush_loop (bounded so a stalled
    # flusher can't grow unbounded — worst case we drop the oldest pending).
    log_persist_queue: collections.deque = collections.deque(maxlen=LOG_PERSIST_QUEUE_MAX)
    handler = LogBufferHandler(log_buffer, log_persist_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("franklinwh_bridge").addHandler(handler)
    logging.getLogger(LIBRARY_LOGGER).addHandler(handler)  # its warnings too (#318)
    configure_console_logging(getattr(logging, config.settings.log_level.upper(), logging.INFO))
    logger.info(
        "FranklinWH Modbus Bridge v%s starting (environment: %s)", __version__, config.environment
    )

    # Before anything else: the notice is only useful if it is the first thing
    # in the log, not buried under a minute of poller output.
    logger.warning(disclaimer.banner())

    db = await init_db(config.db_path)
    await log_startup_event(db, "startup", f"v{__version__} env={config.environment}")
    # Also as a persisted event, so it survives a log-buffer roll and is
    # visible in the Logs tab rather than only on the console at boot.
    await log_startup_event(db, "disclaimer", disclaimer.SHORT)
    await seed_admin(db)  # first-run admin (no lockout); logs a generated pw once
    try:  # first-run date for the support bundle — set once, never overwritten
        await ensure_install_date(db)
    except Exception:  # noqa: BLE001 — must never block startup
        logger.debug("install date not recorded", exc_info=True)

    # MQTT namespace and client id, decided once per install (#87, #118). Before
    # the default gateway is created from the environment: a gateway on file
    # here means an existing install, which keeps its namespace.
    await choose_initial_namespace(db)
    await ensure_client_id(db)

    # Ensure the default gateway exists in the DB. With no host configured it is
    # created UNCONFIGURED (empty host): the registry won't start it, so there
    # is no phantom gateway polling a made-up address.
    gateway_id = "default"
    gw = config.settings.gateway
    async with db.execute(
        "SELECT host, mock FROM gateways WHERE id = ?", (gateway_id,)
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        # A host set AFTER first boot (e.g. the add-on's gateway_host option)
        # used to be ignored, because the row was only ever written once. Fill
        # it in — but only into an unconfigured row, never over a host the user
        # has since set in the UI.
        if not (existing[0] or "").strip() and not existing[1] and gw.host:
            await db.execute(
                "UPDATE gateways SET host = ?, port = ?, unit_id = ?, "
                "poll_interval = ?, description = ? WHERE id = ?",
                (
                    gw.host, gw.port, gw.unit_id, gw.poll_interval,
                    "Auto-created from environment config", gateway_id,
                ),
            )
            await db.commit()
    else:
        await db.execute(
            "INSERT INTO gateways "
            "(id, name, host, port, unit_id, enabled, created_at, "
            " poll_interval, description) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                gateway_id,
                # Named by what it is; the setup wizard renames it from the
                # nameplate ("aGate 0091"). Never "Default Gateway".
                "aGate",
                gw.host,
                gw.port,
                gw.unit_id,
                time.time(),
                gw.poll_interval,
                "Auto-created from environment config" if gw.host
                else UNCONFIGURED_DESCRIPTION,
            ),
        )
        await db.commit()

    stats = await OperationalStats.load(db)
    backup_manager = BackupManager(config.db_path, config.backup_dir)

    # Global sample bus — receives fan-in from all per-gateway buses
    sample_bus = SampleBus()

    app.state.config = config
    app.state.db = db
    # Closed alerts older than 90 days; open ones are never pruned (#324).
    with contextlib.suppress(Exception):
        await alert_store.prune(db)
    app.state.stats = stats
    app.state.backup_manager = backup_manager
    app.state.sample_bus = sample_bus
    app.state.log_buffer = log_buffer

    # ── MQTT Publisher ────────────────────────────────────────
    # Broker settings: environment, then Settings, then (as an add-on) the
    # Supervisor's Mosquitto, then localhost — see config/mqtt_broker.py (#103).
    #
    # As an add-on with no broker yet (Mosquitto not installed), wait for one
    # rather than retry localhost forever: the watcher below connects as soon
    # as the Supervisor reports an MQTT service, with no restart.
    mqtt_config, mqtt_sources = await resolve_broker(await get_mqtt_config(db), discover_mqtt)
    app.state.mqtt_sources = mqtt_sources
    mqtt_broker: dict[str, Any] = {"source": "configured", "waiting_for_broker": False}
    if mqtt_sources["host"] == "supervisor":
        mqtt_broker["source"] = "supervisor"
        logger.info(
            "MQTT broker discovered via Supervisor: %s:%s",
            mqtt_config["host"], mqtt_config["port"],
        )
    elif mqtt_sources["host"] == "default" and supervisor_token():
        mqtt_broker = {"source": "none", "waiting_for_broker": True}
        logger.warning(
            "No MQTT broker yet — install the Mosquitto broker add-on and the "
            "bridge will connect to it automatically. Until then nothing is "
            "published to Home Assistant."
        )
    app.state.mqtt_broker = mqtt_broker

    mqtt_publisher = MqttPublisher.from_db_config(
        mqtt_config, gateway_id=gateway_id,
    )
    app.state.mqtt_publisher = mqtt_publisher
    # Unpublish stays in force across restarts until Republish (#131).
    if (await get_app_config(db, "mqtt_discovery_paused", "0")) == "1":
        mqtt_publisher.set_discovery_paused(True)
        logger.info("MQTT discovery is unpublished (paused) — Republish to restore entities")
    mqtt_publisher.on_published = lambda: mark_published(db)

    await mqtt_publisher.sync_groups(db)

    if mqtt_config.get("enabled", True) and not mqtt_broker["waiting_for_broker"]:
        await mqtt_publisher.start()

    async def _await_supervisor_broker() -> None:
        """Connect to a broker the Supervisor reports after we started."""
        while True:
            await asyncio.sleep(MQTT_WATCH_INTERVAL_S)
            found = await discover_mqtt(quiet=True)
            if not found:
                continue
            await mqtt_publisher.reconfigure({k: v for k, v in found.items() if v is not None})
            # The stored value, not the one read at startup: "Enabled" can be
            # switched while we wait (#96).
            if (await get_mqtt_config(db)).get("enabled", True):
                await mqtt_publisher.start()
            app.state.mqtt_broker = {"source": "supervisor", "waiting_for_broker": False}
            app.state.mqtt_sources = {
                **getattr(app.state, "mqtt_sources", {}),
                **{f: "supervisor" for f in ("host", "port", "username", "password")
                   if found.get(f) is not None},
            }
            logger.info(
                "MQTT broker appeared via Supervisor (%s:%s) — connected",
                found["host"], found["port"],
            )
            return

    mqtt_broker_watch = (
        asyncio.create_task(_await_supervisor_broker())
        if mqtt_broker["waiting_for_broker"] else None
    )

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

    # Point-history cadence state + a cached config (re-read once a minute
    # so a settings change takes effect without a restart).
    _ph_last: dict[str, float] = {}
    _ph_cfg: dict[str, Any] = {"value": None, "loaded_at": 0.0}

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

        # Per-point history (voltage/current/frequency/PF/DC...), when the
        # user has enabled it. Off by default: it can add hundreds of MB to a
        # ~20MB database, so it is opt-in with its own cadence and retention.
        try:
            cfg = _ph_cfg["value"]
            if cfg is None or sample.ts - _ph_cfg["loaded_at"] > 60:
                cfg = await point_history.get_config(db)
                _ph_cfg["value"] = cfg
                _ph_cfg["loaded_at"] = sample.ts
            if cfg["enabled"] and cfg["points"]:
                last = _ph_last.get(sample.gateway_id, 0.0)
                # Its own cadence, independent of the poll rate: the poller may
                # run every 10s while the user asked to keep a point every 60.
                if sample.ts - last >= cfg["interval_s"]:
                    _ph_last[sample.gateway_id] = sample.ts
                    await point_history.record_points(
                        db, sample.points, gateway_id=sample.gateway_id,
                        ts=sample.ts, point_ids=cfg["points"],
                    )
        except Exception as exc:
            logger.debug("Point history record failed: %s", exc)

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
                # Model 702's rating is evidence, not trust (#133): it is used
                # once an observed peak confirms it, or after calibration.
                await inst.command_handler.rating.record_m702(charge_w, discharge_w)
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

            # Point history retires on its own clock — it is far larger per
            # day than `metrics`, so a user may well keep less of it.
            try:
                ph_cfg = await point_history.get_config(db)
                if ph_cfg["enabled"]:
                    await point_history.purge_points(db, ph_cfg["retention_days"])
            except Exception as exc:
                logger.warning("Point history purge failed: %s", exc)

            try:
                await link_stats_store.purge(db)
            except Exception as exc:
                logger.warning("Link stats purge failed: %s", exc)
            await asyncio.sleep(3600)

    purge_task = asyncio.create_task(_metrics_purge_loop())

    # ── Log persistence: drain the queue to SQLite + retention ──
    async def _log_flush_loop() -> None:
        purge_ticks = 0
        while True:
            await asyncio.sleep(LOG_FLUSH_INTERVAL_S)
            workers.beat("log_flush")
            try:
                rows = []
                while log_persist_queue:
                    rows.append(log_persist_queue.popleft())
                if rows:
                    await insert_logs(db, rows)
                await modbus_audit.flush(db)  # the Modbus write audit trail (#342)
            except Exception as exc:
                logger.debug("Log flush failed: %s", exc)
            purge_ticks += 1
            if purge_ticks >= max(1, 3600 // LOG_FLUSH_INTERVAL_S):  # ~hourly
                purge_ticks = 0
                with contextlib.suppress(Exception):
                    await purge_logs(db, time.time() - LOG_RETENTION_DAYS * 86400)
                with contextlib.suppress(Exception):
                    await modbus_audit.prune(db)

    log_flush_task = asyncio.create_task(_log_flush_loop())
    workers.register("log_flush", "Log and audit writes", LOG_FLUSH_INTERVAL_S,
                     task=log_flush_task)

    # ── Link health (Phase 0b): write each closed hour ────────
    # The collector seals an hour in memory when it rolls over; this writes
    # it. A minute's lag on an hourly rollup costs nothing.
    async def _link_stats_loop() -> None:
        while True:
            await asyncio.sleep(60)
            await link_stats_store.flush(db)

    link_stats_task = asyncio.create_task(_link_stats_loop())

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

    # ── Energy totals — period (today/week/month/YTD) kWh from Modbus counters ──
    energy_totals = EnergyTotals(db)
    sample_bus.subscribe(energy_totals.on_sample)
    app.state.energy_totals = energy_totals

    # ── Multi-HA entity access — inbound HA entity states as condition sensors ──
    ha_registry = HaRegistry(db)
    app.state.ha_registry = ha_registry

    # ── User-defined automation constants (min/max/demand SoC) ────────
    constants = ConstantsStore(db)
    await constants.load()
    app.state.constants = constants

    # ── Weather + solar forecast (Open-Meteo, #88) ────────────
    # Off until a location is set. The loop keeps the cache warm so the sync
    # points merge below never waits on the network.
    solar_forecast = SolarForecastService(db)
    await solar_forecast.load()
    app.state.solar_forecast = solar_forecast
    solar_forecast_task = asyncio.create_task(solar_forecast.run())

    # ── Wholesale spot price (AEMO NEM, #89 part A) + HA price entities ──
    # Off until a region is set (the loop fetches nothing). Same pattern as the
    # forecast: the loop warms the cache, the sync points merge only reads it.
    nem_pricing = NemPricingService(db, entity_state=ha_registry.entity_state)
    await nem_pricing.load()
    app.state.nem_pricing = nem_pricing
    nem_pricing_task = asyncio.create_task(nem_pricing.run())

    # ── Utility-service billing/tariff windows (demand.*/bonus.* sensors) ──
    billing = BillingStore(db)
    await billing.load()
    app.state.billing = billing
    # A dynamic plan (#89 part B) is priced from the NEM cache / HA entities:
    # the pricing service needs to know the plan's source (it keeps that
    # region warm and makes tariff.* follow the plan).
    nem_pricing.set_plan_source(billing.dynamic_config)
    nem_pricing.wake()

    # ── Fixed / standing charges accrual (fixed.* sensors) ────
    # Built before the tracker: the billing-period snapshot folds these into
    # net_total, so the tracker needs a reference to it.
    fixed_charges = FixedChargesStore(db)
    await fixed_charges.load()
    app.state.fixed_charges = fixed_charges

    # ── Demand-charge + battery-bonus calculator ──────────────
    demand_tracker = DemandTracker(
        db, billing, gateway_id="default", fixed_charges=fixed_charges,
        live_prices=nem_pricing.dynamic_prices,
    )
    await demand_tracker.load()
    sample_bus.subscribe(demand_tracker.on_sample)
    app.state.demand_tracker = demand_tracker

    def _schedule_points(gw_id: str) -> dict:
        """Latest cached points for a gateway (no Modbus call) + the computed
        period energy totals + HA entity values (``ha:<inst>:<entity>``) + the
        user constants (``const_*``) — feeds the sensor snapshot the engine and
        /api/sensors evaluate condition trees (and derived sensors) against."""
        inst = registry.get(gw_id)
        pts = inst.latest_points() if inst else {}
        # device_type is configuration, not a polled point — a MAC-1 collar
        # can't report "I'm a collar" over Modbus. Merge it in so automations
        # can gate on gateway.device_type / gateway.battery_capable.
        dev_type = getattr(getattr(inst, "config", None), "device_type", None) or "agate"
        # When and how well these points were read, so dispatch supervision only
        # acts on fresh data (design PR #57 §5.1); never a Modbus call.
        last = getattr(getattr(inst, "sample_bus", None), "last_sample", None)
        return {
            **pts,
            "_sample_ts": getattr(last, "ts", None),
            "_sample_quality": getattr(last, "quality", None),
            "device_type": dev_type,
            **energy_totals.current_totals(gw_id),
            **ha_registry.entity_values(),
            **constants.as_points(),
            **billing.as_points(),
            **demand_tracker.as_points(),
            **fixed_charges.as_points(),
            **solar_forecast.as_points(),
            **nem_pricing.as_points(),
        }

    # ── Connectivity monitor — outage detection + (later) catch-up ────
    connectivity = ConnectivityMonitor(db)
    sample_bus.subscribe(connectivity.on_sample)
    app.state.connectivity = connectivity

    def _gw_label(gw_id: str) -> str:
        """Human label for a gateway id in audit targets: name + "(mock)" flag."""
        inst = registry.get(gw_id)
        cfg = getattr(inst, "config", None) if inst else None
        name = getattr(cfg, "name", None) or gw_id
        return f"{name} (mock)" if cfg is not None and getattr(cfg, "mock", False) else str(name)

    schedule_engine = ScheduleEngine(
        db, _schedule_resolver, on_audit=_schedule_audit,
        points_fn=_schedule_points, connectivity=connectivity,
        ha_action_fn=ha_registry.call_service,
        ha_notify_fn=ha_registry.notify,
        gw_label_fn=_gw_label,
    )
    app.state.schedule_engine = schedule_engine

    async def _on_recover(gw_id: str, outage_id: str, start_ts: float, end_ts: float) -> None:
        """On outage recovery, run schedule catch-up and link the missed +
        late-fired jobs to the outage record. Still-open windows resume next tick."""
        result = await schedule_engine.catchup(gw_id, start_ts)
        if result["missed"] or result["late_fired"]:
            await set_outage_catchup(db, outage_id, result["missed"], result["late_fired"])

    connectivity.set_on_recover(_on_recover)

    # ── Health Checker ─────────────────────────────────────────
    health_checker = HealthChecker(registry)
    app.state.health_checker = health_checker
    # Opens and resolves alert conditions; watches the workers (#324 §11).
    alert_monitor = AlertMonitor(
        db, registry, publisher=lambda: getattr(app.state, "mqtt_publisher", None))
    app.state.alert_monitor = alert_monitor
    # Tells HA about critical alerts; dismisses on acknowledgement (#324 §6).
    alert_delivery = AlertDelivery(db, ha_registry)

    # ── Start All Gateways ────────────────────────────────────

    async def wire_default_gateway() -> None:
        """Bind the running default gateway to MQTT, waiting for its device info.

        Runs at startup, and again whenever the default gateway is started
        later — e.g. it was created unconfigured and the user has just set its
        address. Without the second call it would poll happily while publishing
        nothing to HA.
        """
        default = registry.get("default")
        if not default:
            return
        if default.command_handler:
            default.command_handler._on_state_changed = (
                mqtt_publisher.publish_command_state
            )
            mqtt_publisher.set_command_handler(default.command_handler)

        # Wait briefly for the init task to discover device info. If the
        # aGate is slow or reconnecting we must NOT give up quietly — that
        # left the publisher unbound and every real sample was dropped
        # while the mock kept publishing, with nothing in the log to say so.
        wired = False
        for _ in range(20):
            if await wire_default_mqtt():
                wired = True
                break
            await asyncio.sleep(0.5)

        if not wired:
            logger.warning(
                "Default gateway device info not available after 10s — MQTT "
                "publishing for it is INACTIVE until it is. Retrying in the "
                "background; HA entities for the real gateway will be stale "
                "until this succeeds."
            )

            async def _retry_wire_default() -> None:
                # Bounded: ~10 minutes. A gateway that never reports device
                # info is a connectivity problem, not something to poll for
                # ever, and the warning above has already been logged.
                for _ in range(120):
                    await asyncio.sleep(5)
                    if await wire_default_mqtt():
                        logger.info(
                            "Default gateway MQTT wiring recovered — publishing resumed"
                        )
                        return
                logger.error(
                    "Default gateway never reported device info — its MQTT "
                    "entities will not update. Check the gateway connection."
                )

            asyncio.create_task(_retry_wire_default())

        # Wire reader_fn for POST /api/models/refresh
        if default.reader_fn:
            app.state.reader_fn = default.reader_fn

    app.state.wire_default_gateway = wire_default_gateway

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

        await wire_default_gateway()

        await sync_mqtt_devices()

    async def wire_default_mqtt() -> bool:
        """Bind the default gateway's device info to the publisher. Idempotent.

        The default gateway does NOT go through register_device() — it keeps the
        legacy single-device path so its topics and unique_ids never move. That
        path is gated on ``self._device_info`` being set, and this is the only
        thing that sets it.

        Two ways that used to fail silently, both of which dropped every real
        sample while the mock kept publishing:

        1. Startup raced the aGate. The caller polled for 10s and, on a slow or
           reconnecting gateway, simply fell out of the loop — no else branch,
           no warning, no retry — leaving _device_info None for the life of the
           process. queue_sample then took its "unregistered gateway" branch
           and discarded the samples.
        2. A gateway restart re-created the instance with fresh device_info,
           but the wiring only ever ran once during lifespan startup, so the
           publisher kept a stale binding or none at all.

        Now callable repeatedly and from anywhere a gateway (re)starts.

        Honours the gateway's "Publish to Home Assistant" (#96). Off: its
        discovery is tombstoned (if it was bound) and it is unbound, so nothing
        of it reaches HA — and True is returned, since there is nothing left
        to wait for. On: bound as before, which re-publishes discovery.
        """
        gw_row = None
        try:
            gw_row = await get_gateway(db, "default")
        except Exception as exc:
            logger.debug("default gateway lookup failed: %s", exc)
        if gw_row is not None and not gw_row.get("publish_to_ha", 1):
            # Tombstones are queued only while bound, so repeat calls are quiet.
            mqtt_publisher.unbind_default_device(tombstone=True)
            return True
        mqtt_publisher.resume_default_device()

        inst = registry.get("default")
        if inst is None or not inst.device_info:
            return False
        mqtt_publisher.set_device_info(inst.device_info)
        mqtt_publisher.set_ac_type(inst.status.ac_type)
        if inst.command_handler:
            inst.command_handler._on_state_changed = mqtt_publisher.publish_command_state
            mqtt_publisher.set_command_handler(inst.command_handler)
        if gw_row:
            mqtt_publisher.set_phase_view(gw_row.get("phase_view", "both"))
        return True

    app.state.wire_default_mqtt = wire_default_mqtt

    async def sync_mqtt_devices() -> None:
        """Give every opted-in NON-DEFAULT gateway its own MQTT/HA device.

        Until this existed, register_device() was never called, so additional
        gateways fell through to the default gateway's topics and overwrote its
        values — a mock was driving a real battery's SoC sensor (fixed in
        queue_sample, which now drops unregistered gateways).

        The default gateway deliberately keeps the legacy single-device path:
        its topics and unique_ids must not move, or every existing HA entity
        would be orphaned and re-created.

        Safe to call repeatedly — used at startup and after a gateway is added,
        edited or toggled.
        """
        # The default gateway is wired here too, not just at startup: a restart
        # replaces the instance, and without this the publisher keeps a stale
        # binding (or none) and silently drops every real sample.
        await wire_default_mqtt()

        try:
            rows = {g["id"]: g for g in await get_gateways(db)}
        except Exception as exc:
            logger.warning("MQTT device sync skipped (gateway read failed): %s", exc)
            return

        for gw_id, row in rows.items():
            if gw_id == gateway_id:
                continue  # legacy single-device path owns the default gateway
            inst = registry.get(gw_id)
            wanted = bool(row.get("publish_to_ha", 1)) and bool(row.get("enabled", 1))
            registered = mqtt_publisher.get_device(gw_id) is not None

            if wanted and registered and inst and inst.device_info:
                # A gateway restart or reconnect replaces its instance: the
                # registration must follow it, or the device keeps the old
                # command handler and device info (#128). Re-bind without a
                # tombstone — same entities, nothing for HA to remove.
                dev = mqtt_publisher.get_device(gw_id)
                if (dev.command_handler is not inst.command_handler
                        or dev.device_info is not inst.device_info):
                    await mqtt_publisher.unregister_device(gw_id, tombstone=False)
                    registered = False

            if wanted and inst and inst.device_info and not registered:
                logger.info("MQTT device registered for gateway %s", gw_id)
                mqtt_publisher.register_device(
                    gw_id,
                    inst.device_info,
                    command_handler=inst.command_handler,
                    ac_type=inst.status.ac_type or 0,
                )
            elif registered and not wanted:
                # Tombstone discovery so HA removes the entities rather than
                # leaving them behind as permanently-unavailable.
                await mqtt_publisher.unregister_device(gw_id, tombstone=True)

        # A gateway removed from the DB entirely must not linger in MQTT.
        for gw_id in [g for g in mqtt_publisher.devices() if g not in rows]:
            await mqtt_publisher.unregister_device(gw_id, tombstone=True)

    app.state.sync_mqtt_devices = sync_mqtt_devices

    def _mqtt_devices_out_of_sync(rows: dict[str, dict]) -> bool:
        """Any opted-in extra gateway whose device info is known but whose MQTT
        registration is missing or bound to a replaced instance."""
        for gw_id, row in rows.items():
            if gw_id == gateway_id:
                continue
            if not (row.get("publish_to_ha", 1) and row.get("enabled", 1)):
                continue
            inst = registry.get(gw_id)
            if not (inst and inst.device_info):
                continue
            dev = mqtt_publisher.get_device(gw_id)
            if dev is None or dev.device_info is not inst.device_info:
                return True
        return False

    async def _reconcile_mqtt_devices() -> None:
        """Register extra gateways as soon as their device info is known (#128).

        sync_mqtt_devices() ran once at startup, before a real aGate had been
        read, and never again — so with three gateways only the primary reached
        Home Assistant and the others' samples were dropped. This re-checks
        every 15 s (cheap: no I/O unless something changed) and covers late
        connects, reconnects and restarts.
        """
        while True:
            await asyncio.sleep(15)
            try:
                rows = {g["id"]: g for g in await get_gateways(db)}
                if _mqtt_devices_out_of_sync(rows):
                    await sync_mqtt_devices()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("MQTT device reconcile failed: %s", exc)

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
            await alert_monitor.start()
        except Exception as exc:
            logger.error("Alert monitor start failed: %s", exc)

        try:
            # The schedule engine no-ops on targets with no running handler, so
            # a late-arriving or failed gateway is fine.
            await schedule_engine.start()
        except Exception as exc:
            logger.error("Schedule engine start failed: %s", exc)

        try:
            # As an add-on, the HA we run under is an entity source with no
            # setup at all (Supervisor proxy + token). Inert elsewhere.
            await ensure_local_ha_instance(db)
        except Exception as exc:
            logger.warning("HA auto-configuration via the Supervisor failed: %s", exc)

        try:
            # Multi-HA: start polling configured HA instances (no-op if none).
            await ha_registry.start()
        except Exception as exc:
            logger.warning("HA registry start failed: %s", exc)

        try:
            # After the HA registry, so the first pass knows where HA is.
            await alert_delivery.start()
        except Exception as exc:
            logger.error("Alert delivery start failed: %s", exc)

        try:
            # Startup catch-up: if the Bridge was down across a gap (persisted
            # last-good-poll per gateway), record the downtime and catch up any
            # fires missed while offline. Runs after the engine has loaded entries.
            await connectivity.startup_catchup(schedule_engine)
        except Exception as exc:
            logger.warning("Startup catch-up failed: %s", exc)

        try:
            await log_metrics_snapshot(db, "metrics_snapshot_startup")
        except Exception as exc:
            logger.warning("Metrics snapshot (startup) failed: %s", exc)

    asyncio.create_task(_start_gateways())
    mqtt_device_reconcile = asyncio.create_task(_reconcile_mqtt_devices())

    async def _clean_up_leftovers() -> None:
        """Once, after gateways have connected and published: remove this
        bridge's own stale discovery configs and empty old HA devices, so users
        don't delete them by hand (publish/cleanup.py)."""
        await asyncio.sleep(90)
        try:
            from franklinwh_bridge.publish import cleanup

            await cleanup.sweep_broker(mqtt_publisher)
            rows = await get_gateways(db)
            idents = cleanup.old_identifiers(
                mqtt_publisher.topic_prefix,
                [(g["id"], g.get("serial") or "") for g in rows if not g.get("mock")],
            )
            # Whichever HA reads this broker has the old devices; check each
            # connected one — nothing matches where they don't exist.
            for row in ha_registry.status():
                if row.get("connected"):
                    await cleanup.remove_empty_old_devices(ha_registry.get(row["id"]), idents)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Leftover clean-up failed: %s", exc)

    leftover_cleanup = asyncio.create_task(_clean_up_leftovers())

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

    # Take the host timezone from the Supervisor BEFORE reading the clock or
    # recording the expected zone. run.sh asks bashio for this, but on a real
    # add-on install that call returns "forbidden" and its `> /dev/null` guard
    # cannot tell that from "not configured" — so the container silently stays
    # on UTC. If check_and_record() then ran first it would record UTC as the
    # expected zone and confirm the wrong clock, turning a caught bug into a
    # blessed one. Inert outside an add-on.
    try:
        applied_tz = await apply_timezone()
        if applied_tz:
            logger.info(
                "Timezone taken from the Supervisor: %s (run.sh had left TZ=%s)",
                applied_tz, "unset/UTC",
            )
    except Exception as exc:  # never block startup on auto-config
        logger.warning("Supervisor timezone auto-config failed: %s", exc)

    # Schedule triggers and TOU windows run on this clock — log it, then check
    # it against the timezone recorded at install so a drift is caught at boot
    # rather than by a missed dispatch hours later.
    _lt = time.localtime()
    logger.info(
        "Local clock: %s %s (UTC%+.2g) — schedules and TOU windows use this",
        time.strftime("%Y-%m-%d %H:%M:%S", _lt), _lt.tm_zone, _lt.tm_gmtoff / 3600,
    )
    try:
        app.state.timezone_check = await check_and_record(db)
    except Exception as exc:  # never block startup on the guard itself
        logger.warning("Timezone check failed: %s", exc)
        app.state.timezone_check = None

    yield

    # ── Shutdown ──────────────────────────────────────────────
    logger.info("Bridge shutting down — releasing control and logging state")

    # 1. Stop the schedule engine (so it can't re-dispatch during teardown),
    #    then the health checker.
    await schedule_engine.stop()
    await alert_monitor.stop()
    await alert_delivery.stop()
    await health_checker.stop()
    await ha_registry.stop()
    solar_forecast_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await solar_forecast_task
    nem_pricing_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await nem_pricing_task
    if mqtt_broker_watch is not None and not mqtt_broker_watch.done():
        mqtt_broker_watch.cancel()
    if not mqtt_device_reconcile.done():
        mqtt_device_reconcile.cancel()
    if not leftover_cleanup.done():
        leftover_cleanup.cancel()

    # 2. Stop all gateways (releases commands, stops pollers, disconnects)
    await registry.stop_all()

    # 2a. Write link health, including the partial hour (after the gateways
    #     stop, so their release and disconnect are in it).
    link_stats_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await link_stats_task
    await link_stats_store.flush(db, final=True)

    # 2. Cancel metrics purge task
    if purge_task and not purge_task.done():
        purge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purge_task

    # 2b. Stop the log flusher, then do a final drain so shutdown logs persist.
    if log_flush_task and not log_flush_task.done():
        log_flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_flush_task
    with contextlib.suppress(Exception):
        await modbus_audit.flush(db)  # the last writes, e.g. the shutdown release
    with contextlib.suppress(Exception):
        final_rows = []
        while log_persist_queue:
            final_rows.append(log_persist_queue.popleft())
        if final_rows:
            await insert_logs(db, final_rows)

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
    # Carried in the OpenAPI schema, so anyone reaching the API through /docs
    # or a generated client sees the notice — not only readers of the README.
    description=disclaimer.MARKDOWN,
)

# Signed-cookie sessions (Starlette). `Secure` when SESSION_COOKIE_SECURE=1
# (set it behind TLS); SameSite=Lax mitigates CSRF on cookie-auth'd writes.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret_key(),
    same_site="lax",
    https_only=os.environ.get("SESSION_COOKIE_SECURE") == "1",
)

# Mount static files
_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Open routers: health/status probes + auth (login is how you get a session).
app.include_router(health_router)
app.include_router(auth_router)

# Authenticated routers — require a logged-in user (401 otherwise). Under HA
# ingress the Supervisor already authed, so require_auth returns a synthetic
# admin. Feature routers ALSO carry their module's enabled gate.
#
# Writes on these routers also need the admin role (#102): before, any signed-in
# viewer could reconfigure the bridge. A write route with its own role or
# capability gate (the per-gateway command needs `control`) keeps that gate.
_ADMIN_WRITES = Depends(require_admin_for_writes)
_AUTH = [Depends(require_auth), _ADMIN_WRITES]
app.include_router(admin_router, dependencies=_AUTH)
app.include_router(mqtt_router, dependencies=_AUTH)
app.include_router(groups_router, dependencies=_AUTH)
app.include_router(gateways_router, dependencies=_AUTH)
app.include_router(setup_router, dependencies=_AUTH)
app.include_router(modules_router, dependencies=_AUTH)
app.include_router(energy_router, dependencies=_AUTH)
app.include_router(point_history_router, dependencies=_AUTH)
app.include_router(solar_router, dependencies=_AUTH)
# Not behind the energy_costs module: the region feeds condition sensors and is
# set on the Settings page whether or not the Energy Costs tab is on.
app.include_router(nem_router, dependencies=_AUTH)
# No _AUTH here: the router's own deps already require a user, and the GET
# must stay reachable for any signed-in role so the modal can render.
app.include_router(disclaimer_router)
app.include_router(users_router)  # admin-only via its own require_role dep
app.include_router(alerts_router)  # GET any signed-in role; ack admin-only (#324)
app.include_router(diagnostics_router)  # admin-only via its own require_role dep (#342)
app.include_router(support_router)  # admin-only via its own require_role dep
app.include_router(
    reader_router, dependencies=[require_module("explorer")],
)  # admin-only via its own require_role dep (#288)
app.include_router(
    schedules_router,
    dependencies=[require_module("automations"), Depends(require_auth), _ADMIN_WRITES],
)
app.include_router(
    scheduler_router,
    dependencies=[require_module("automations"), Depends(require_auth), _ADMIN_WRITES],
)
app.include_router(
    ha_router, dependencies=[require_module("ha_entities"), Depends(require_auth), _ADMIN_WRITES],
)
app.include_router(
    tariff_router,
    dependencies=[require_module("energy_costs"), Depends(require_auth), _ADMIN_WRITES],
)

# UI router (serves GET / and POST /api/command). GET / redirects to /login when
# unauthenticated; /api/command guards itself.
app.include_router(ui_router)
