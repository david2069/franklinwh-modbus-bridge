"""Record unmapped extension registers' changes with context, to map them (#36).

15016 is read every poll (``vreg_15016``) but has no confirmed meaning:
- franklinwh-modbus VERIFICATION_BASELINE: "OnGridMode? Low — same value as 15507"
  (both 2 at the time);
- an unverified community report (franklinwh-modbus#18) claims "power source,
  2 grid / 0 islanded / 3 generator" — a lead, not evidence;
- the reference site read 6 on 2026-10-09 in Self-Consumption, which fits neither.

Each change is logged (persisted with the app logs) alongside the state that
might explain it, so the history can be correlated before anything is named.
Nothing decides on these values.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

WATCHED = (15016,)

# State a candidate meaning would have to line up with.
_CONTEXT_KEYS = (
    "mode_name", "grid_mode", "battery_state", "battery_power_w", "grid_power_w",
    "total_solar", "soc", "wset_enabled", "wset_pct", "connection_state",
)


class RegisterWatch:
    """Log each change of the watched registers, with context."""

    def __init__(self, gateway_id: str, registers: tuple[int, ...] = WATCHED) -> None:
        self._gateway_id = gateway_id
        self._registers = registers
        self._last: dict[int, Any] = {}

    def apply(self, points: dict[str, Any]) -> None:
        for reg in self._registers:
            value = points.get(f"vreg_{reg}")
            if value is None:
                continue  # not read this poll — not a change
            prev = self._last.get(reg)
            if value == prev:
                continue
            self._last[reg] = value
            context = ", ".join(
                f"{k}={points[k]}" for k in _CONTEXT_KEYS if points.get(k) is not None
            )
            logger.info(
                "Gateway %s: unmapped register %d %s → %s (%s)",
                self._gateway_id, reg, "start" if prev is None else prev, value, context,
            )
