"""Reserve SoC points (#34, #256).

``self_reserve_pct`` (15508) and ``tou_reserve_pct`` (15509) are published as
the aGate reports them. ``active_reserve_pct`` is the active mode's register,
cross-checked against 16001; in Emergency Backup it is 16001 itself (#311).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def is_tou(mode: object) -> bool:
    m = str(mode or "").lower()
    return m == "tou" or "time of use" in m or "time-of-use" in m


def is_self_consumption(mode: object) -> bool:
    return "self" in str(mode or "").lower()


def apply_active_reserve(points: dict[str, Any], active: float | int | None) -> None:
    """Set ``active_reserve_pct``. ``self_reserve_pct`` (15508) and
    ``tou_reserve_pct`` (15509) are left exactly as the aGate reported them
    (owner, 2026-10-19: report what the registers report)."""
    points["active_reserve_pct"] = active


class ActiveReserveReader:
    """Derive the active reserve from a real poll.

    Self-Consumption: 15508. TOU: 15509. Both cross-checked against 16001, which
    reads the active mode's reserve (owner's aGate, V12R02B60D00, 2026-10-21:
    SC 8, TOU 15, Emergency Backup 100). Any other mode (Emergency Backup) has
    no reserve register of its own, so it is 16001 as read (#311).
    """

    def __init__(self, gateway_id: str) -> None:
        self._gateway_id = gateway_id
        self._last_mismatch: tuple | None = None

    def apply(self, points: dict[str, Any]) -> None:
        if "self_reserve_pct" not in points and "tou_reserve_pct" not in points:
            return  # not read this poll: add nothing (an empty poll stays empty)
        # The registers as read, before any interpretation, so the Explorer
        # always shows them (#256). #34's reading rests on evidence taken while
        # the library wrote 15507-15509 on every connect (#239); these raw
        # values are what re-checks it.
        for key, raw in (("vreg_15508", "self_reserve_pct"), ("vreg_15509", "tou_reserve_pct")):
            if points.get(raw) is not None:
                points[key] = points[raw]
        # The active mode's own register; 16001 where the mode has none (#311).
        mode = points.get("mode_name")
        cross = points.get("vreg_16001")
        if is_tou(mode):
            active, reg = points.get("tou_reserve_pct"), "15509"
        elif is_self_consumption(mode):
            active, reg = points.get("self_reserve_pct"), "15508"
        else:
            active, reg = cross, None
        if reg and active is not None and cross is not None and int(cross) != int(active):
            pair = (int(active), int(cross))
            if pair != self._last_mismatch:  # once per distinct disagreement
                logger.warning(
                    "Gateway %s: active reserve %s=%s disagrees with 16001=%s",
                    self._gateway_id, reg, active, cross,
                )
                self._last_mismatch = pair
        elif self._last_mismatch is not None:
            self._last_mismatch = None
        apply_active_reserve(points, active)
