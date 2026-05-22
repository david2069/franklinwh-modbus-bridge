"""FastAPI application with staged lifespan startup."""

from __future__ import annotations

import asyncio
import collections
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from franklinwh_bridge import __version__
from franklinwh_bridge.api.admin import router as admin_router
from franklinwh_bridge.api.health import register_component
from franklinwh_bridge.api.health import router as health_router
from franklinwh_bridge.api.mqtt_api import router as mqtt_router
from franklinwh_bridge.config.manager import AppConfig
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher
from franklinwh_bridge.store.db import get_mqtt_config, init_db, log_startup_event

logger = logging.getLogger(__name__)

LOG_BUFFER_SIZE = 500


class LogBufferHandler(logging.Handler):
    """In-memory ring buffer for the /api/logs endpoint."""

    def __init__(self, buffer: collections.deque):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append({
            "ts": record.created,
            "level": record.levelname,
            "name": record.name,
            "message": self.format(record),
        })


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

    sample_bus = SampleBus()

    app.state.config = config
    app.state.db = db
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

    if mqtt_config.get("enabled", True):
        await mqtt_publisher.start()

    # Create controller and poller
    poller: ModbusPoller | None = None
    try:
        from franklinwh_modbus import FranklinWHController

        gw = config.settings.gateway
        controller = FranklinWHController(
            ip_address=gw.host, port=gw.port, unit_id=gw.unit_id
        )

        poller = ModbusPoller(
            controller=controller,
            sample_bus=sample_bus,
            gateway_id=gateway_id,
            poll_interval=gw.poll_interval,
        )
        app.state.poller = poller

        sample_bus.subscribe(mqtt_publisher.queue_sample)

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
                await asyncio.to_thread(controller.disconnect)

            await poller.start()

        asyncio.create_task(_init_poller())

    except Exception as exc:
        logger.warning("Poller init failed (no hardware?): %s", exc)

    register_component("poller", lambda: {
        "status": "running" if poller and poller.state.connected else "disconnected",
        "polls_total": poller.state.polls_total if poller else 0,
        "last_poll_ts": poller.state.last_poll_ts if poller else None,
        "last_error": poller.state.last_error if poller else None,
    } if poller else {"status": "not_configured"})
    register_component("mqtt", lambda: {
        "connected": mqtt_publisher.state.connected,
        "messages_sent": mqtt_publisher.state.messages_sent,
        "discovery_published": mqtt_publisher.state.discovery_published,
    })

    logger.info("Bridge started (env=%s, v%s)", config.environment, __version__)

    yield

    if poller:
        await poller.stop()
    await mqtt_publisher.stop()
    await db.close()
    logger.info("Bridge shutdown complete")


app = FastAPI(
    title="franklinwh-modbus-bridge",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(admin_router)
app.include_router(mqtt_router)
