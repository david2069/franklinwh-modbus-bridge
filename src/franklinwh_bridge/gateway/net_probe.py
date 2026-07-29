"""Shared raw-socket TCP reachability probe.

Used independently by the periodic HealthChecker, the on-demand
"test connection" endpoint, and the diagnostics tool — all three just
want "can I open a TCP connection to host:port, and how long did it take."
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 5.0


@dataclass
class ProbeResult:
    ok: bool
    host: str
    port: int
    latency_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "host": self.host,
            "port": self.port,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


async def tcp_probe(host: str, port: int, timeout: float = DEFAULT_TIMEOUT_S) -> ProbeResult:
    """Attempt a raw TCP connection to host:port. Never raises."""

    def _connect() -> float:
        t0 = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        finally:
            sock.close()
        return time.monotonic() - t0

    try:
        elapsed = await asyncio.wait_for(
            asyncio.to_thread(_connect), timeout=timeout + 1,
        )
        return ProbeResult(ok=True, host=host, port=port, latency_ms=round(elapsed * 1000, 1))
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return ProbeResult(ok=False, host=host, port=port, error=str(exc) or type(exc).__name__)
