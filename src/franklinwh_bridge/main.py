"""FastAPI application with staged lifespan startup."""

from __future__ import annotations

import collections
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from franklinwh_bridge import __version__
from franklinwh_bridge.api.admin import router as admin_router
from franklinwh_bridge.api.health import register_component
from franklinwh_bridge.api.health import router as health_router
from franklinwh_bridge.config.manager import AppConfig
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.store.db import init_db, log_startup_event

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

    register_component("poller", lambda: {"status": "not_started"})
    register_component("mqtt", lambda: {"status": "not_configured"})

    logger.info("Bridge started (env=%s, v%s)", config.environment, __version__)

    yield

    await db.close()
    logger.info("Bridge shutdown complete")


app = FastAPI(
    title="franklinwh-modbus-bridge",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(admin_router)
