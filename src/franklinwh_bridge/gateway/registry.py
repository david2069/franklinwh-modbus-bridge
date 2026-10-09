"""GatewayRegistry — manages per-gateway instances.

The registry loads gateway configurations from the database, creates
``GatewayInstance`` objects for each enabled gateway, and provides
lookup/lifecycle methods.  It replaces the single-gateway pattern in
main.py's lifespan with a scalable multi-gateway architecture.

Backward compatible: a "default" gateway is auto-created on first run
from environment configuration.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from franklinwh_bridge.gateway.instance import GatewayConfig, GatewayInstance
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.store.db import (
    gateway_is_unconfigured,
    get_gateway,
    get_gateways,
)

logger = logging.getLogger(__name__)


class GatewayRegistry:
    """Central registry for all gateway instances."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        global_bus: SampleBus,
        stats: Any | None = None,
    ) -> None:
        self._db = db
        self._global_bus = global_bus
        self._stats = stats
        self._instances: dict[str, GatewayInstance] = {}

    @property
    def instances(self) -> dict[str, GatewayInstance]:
        return self._instances

    def get(self, gateway_id: str) -> GatewayInstance | None:
        """Get a running gateway instance by ID."""
        return self._instances.get(gateway_id)

    def get_default(self) -> GatewayInstance | None:
        """Get the default gateway instance."""
        return self._instances.get("default")

    def list_active(self) -> list[str]:
        """Return IDs of all active (started) gateways."""
        return [gw_id for gw_id, inst in self._instances.items() if inst.is_running]

    def list_all(self) -> list[str]:
        """Return IDs of all registered gateways."""
        return list(self._instances.keys())

    async def start_gateway(self, gateway_id: str) -> GatewayInstance | None:
        """Start a single gateway by loading its config from the DB."""
        if gateway_id in self._instances:
            logger.warning("Gateway %s already registered, stopping first", gateway_id)
            await self.stop_gateway(gateway_id)

        gw_row = await get_gateway(self._db, gateway_id)
        if gw_row is None:
            logger.error("Gateway %s not found in database", gateway_id)
            return None

        if not gw_row.get("enabled", True):
            logger.info("Gateway %s is disabled, skipping", gateway_id)
            return None

        if gateway_is_unconfigured(gw_row):
            # No address to connect to. Starting it would only produce an
            # endless stream of connection timeouts and an "unreachable" alarm.
            logger.info(
                "Gateway %s has no host configured, not starting it", gateway_id,
            )
            return None

        config = GatewayConfig(
            gateway_id=gw_row["id"],
            name=gw_row["name"],
            host=gw_row["host"],
            port=gw_row["port"],
            unit_id=gw_row.get("unit_id", 1),
            poll_interval=gw_row.get("poll_interval", 10),
            timeout=gw_row.get("timeout", 10.0),
            description=gw_row.get("description", ""),
            enabled=bool(gw_row.get("enabled", True)),
            mock=bool(gw_row.get("mock", 0)),
            service_id=gw_row.get("service_id"),
            device_type=gw_row.get("device_type") or "agate",
            ac_type=int(gw_row.get("ac_type") or 0),
            home_load_source=gw_row.get("home_load_source") or "standard",
        )

        instance = GatewayInstance(
            config=config,
            db=self._db,
            global_bus=self._global_bus,
            stats=self._stats,
        )
        self._instances[gateway_id] = instance
        await instance.start()
        return instance

    async def stop_gateway(self, gateway_id: str) -> None:
        """Stop and unregister a gateway instance."""
        instance = self._instances.pop(gateway_id, None)
        if instance:
            await instance.stop()
            logger.info("Gateway %s unregistered", gateway_id)

    async def start_all(self) -> None:
        """Start all enabled gateways from the database."""
        gateways = await get_gateways(self._db)
        started = 0
        for gw_row in gateways:
            if not gw_row.get("enabled", True):
                continue
            # Honour a user "Stop": autostart=0 means the user paused this
            # gateway, so it should NOT come back on app boot.  (enabled is
            # still 1 — it's a valid gateway, just not auto-started.)
            if not gw_row.get("autostart", 1):
                logger.info(
                    "Gateway %s autostart disabled (user-stopped), skipping",
                    gw_row["id"],
                )
                continue
            try:
                inst = await self.start_gateway(gw_row["id"])
            except Exception as exc:
                # One gateway's connection failure must not abort starting the
                # rest (or the health checker / schedule engine downstream).
                logger.error(
                    "Gateway %s failed to start: %s", gw_row["id"], exc,
                )
                continue
            if inst:
                started += 1
        logger.info(
            "Gateway registry: %d of %d gateways started",
            started, len(gateways),
        )

    async def stop_all(self) -> None:
        """Stop all running gateway instances."""
        gateway_ids = list(self._instances.keys())
        for gw_id in gateway_ids:
            await self.stop_gateway(gw_id)
        logger.info("All gateways stopped")

    def status_all(self) -> list[dict]:
        """Return status dicts for all registered gateways."""
        return [inst.to_dict() for inst in self._instances.values()]
