"""User-defined automation constants — a small in-memory + app_config-backed
store for the SOC parameters that automations (and the derived sensors) use but
that can't be read from the aGate: min-discharge / max-charge / demand-charge
SoC. Kept in memory so the (sync) points-merge can read them each tick without a
DB round-trip; persisted to ``app_config`` on change.

Exposed to the sensor layer as ``const_<key>`` point keys (merged into the
gateway points), which the ``const.*`` and time-to-* derived sensors read.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from franklinwh_bridge.store.db import get_app_config, set_app_config

logger = logging.getLogger(__name__)

_KEY = "automation_constants"

#: name -> (default, min, max). SoC percentages.
_SPEC: dict[str, tuple[float, float, float]] = {
    "min_discharge_soc": (20.0, 0.0, 100.0),
    "max_charge_soc": (100.0, 0.0, 100.0),
    "demand_charge_min_soc": (30.0, 0.0, 100.0),
}


class ConstantsStore:
    """Holds the user constants in memory; loads/persists via app_config."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._values: dict[str, float] = {k: v[0] for k, v in _SPEC.items()}

    async def load(self) -> None:
        try:
            raw = await get_app_config(self._db, _KEY)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("ConstantsStore load failed: %s", exc)
            return
        if raw:
            try:
                stored = json.loads(raw)
            except (ValueError, TypeError):
                return
            for k in _SPEC:
                if k in stored:
                    self._values[k] = float(stored[k])

    def values(self) -> dict[str, float]:
        return dict(self._values)

    @staticmethod
    def spec() -> dict[str, tuple[float, float, float]]:
        return dict(_SPEC)

    async def update(self, updates: dict[str, Any]) -> dict[str, float]:
        """Apply + persist known keys, each clamped to its [min, max]. Unknown
        keys are ignored; invalid values raise ValueError."""
        for k, v in updates.items():
            if k not in _SPEC:
                continue
            fv = float(v)  # raises ValueError on non-numeric
            lo, hi = _SPEC[k][1], _SPEC[k][2]
            self._values[k] = max(lo, min(hi, fv))
        await set_app_config(self._db, _KEY, json.dumps(self._values))
        return self.values()

    def as_points(self) -> dict[str, float]:
        """Constants as ``const_<key>`` point keys for the sensor snapshot."""
        return {f"const_{k}": v for k, v in self._values.items()}
