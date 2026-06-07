"""Gateway health checker — periodic TCP probes independent of the poller.

Detects three states per gateway:
- ``connected``: TCP + Modbus reads succeeding (poller is healthy)
- ``tcp_only``: TCP port reachable but Modbus reads failing
- ``unreachable``: TCP connection refused or timed out

Runs as a background asyncio task, probing all registered gateways
every ``check_interval_s`` seconds (default 60).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franklinwh_bridge.gateway.registry import GatewayRegistry

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_S = 60
TCP_TIMEOUT_S = 5


class HealthChecker:
    """Periodic TCP probe for all registered gateways."""

    def __init__(
        self,
        registry: GatewayRegistry,
        check_interval_s: int = DEFAULT_CHECK_INTERVAL_S,
    ) -> None:
        self._registry = registry
        self._check_interval = check_interval_s
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("Health checker started (interval=%ds)", self._check_interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await self._check_all()
                await asyncio.sleep(self._check_interval)
        except asyncio.CancelledError:
            pass

    async def _check_all(self) -> None:
        """Probe every registered gateway's TCP port."""
        for gw_id, inst in self._registry.instances.items():
            if not inst.config.enabled:
                continue

            host = inst.config.host
            port = inst.config.port
            old_health = inst.status.health

            tcp_ok = await self._tcp_probe(host, port)

            if not tcp_ok:
                inst.status.health = "unreachable"
            elif inst.status.connected and inst.status.polling:
                inst.status.health = "connected"
            else:
                inst.status.health = "tcp_only"

            if inst.status.health != old_health:
                logger.info(
                    "Gateway %s health: %s → %s (%s:%d)",
                    gw_id, old_health, inst.status.health, host, port,
                )

    @staticmethod
    async def _tcp_probe(host: str, port: int) -> bool:
        """Attempt a TCP connection. Returns True if the port is reachable."""

        def _connect() -> bool:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(TCP_TIMEOUT_S)
            try:
                sock.connect((host, port))
                return True
            except (ConnectionRefusedError, OSError, TimeoutError):
                return False
            finally:
                sock.close()

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_connect), timeout=TCP_TIMEOUT_S + 1,
            )
        except TimeoutError:
            return False

    async def check_one(self, gateway_id: str) -> str:
        """Probe a single gateway and return the health status string."""
        inst = self._registry.get(gateway_id)
        if inst is None:
            return "unknown"

        tcp_ok = await self._tcp_probe(inst.config.host, inst.config.port)
        if not tcp_ok:
            inst.status.health = "unreachable"
        elif inst.status.connected and inst.status.polling:
            inst.status.health = "connected"
        else:
            inst.status.health = "tcp_only"
        return inst.status.health
