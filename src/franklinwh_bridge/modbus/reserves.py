"""Reserve SoC points under the documented 15508/15509 quirk (#34).

Documented (franklinwh-modbus docs/FRANKLINWH_SUNSPEC_QUIRKS.md, "SOC Reserve
Registers — Known Defect"; vendor PICS SPAN tab): 15508 (Self-Consumption
reserve) and 15509 (TOU reserve) always return the same value, so at least one
of them is not the reserve its name says.

UNCONFIRMED working reading: the shared value is the reserve of the mode
currently active. The only evidence is the reference site in Self-Consumption
(app SC 5 / TOU 15, registers 5): consistent with it, not proof. An unverified
community report (franklinwh-modbus#18) claims the same; it is a lead, not
evidence. To verify: a mode switch, or reserves set apart, on the reference site.

Publishing on that reading is still the safer choice: a per-mode reserve is shown
only while its mode is active (else unknown), which avoids the documented wrong
value either way; ``active_reserve_pct`` is "the shared value", cross-checked
against 16001 (also unconfirmed).
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
    """Set ``active_reserve_pct`` and gate the per-mode reserve points.

    ``self_reserve_pct`` / ``tou_reserve_pct`` keep a value only while their own
    mode is active; otherwise they become None (unknown). Emergency Backup and
    unknown modes leave both unknown.
    """
    points["active_reserve_pct"] = active
    mode = points.get("mode_name")
    points["self_reserve_pct"] = active if is_self_consumption(mode) else None
    points["tou_reserve_pct"] = active if is_tou(mode) else None


class ActiveReserveReader:
    """Derive the active reserve from a real poll (15508, cross-checked vs 16001)."""

    def __init__(self, gateway_id: str) -> None:
        self._gateway_id = gateway_id
        self._last_mismatch: tuple | None = None

    def apply(self, points: dict[str, Any]) -> None:
        if "self_reserve_pct" not in points and "tou_reserve_pct" not in points:
            return  # not read this poll: add nothing (an empty poll stays empty)
        active = points.get("self_reserve_pct")  # 15508 = the active reserve
        if active is None:
            active = points.get("tou_reserve_pct")
        cross = points.get("vreg_16001")
        if active is not None and cross is not None and int(cross) != int(active):
            pair = (int(active), int(cross))
            if pair != self._last_mismatch:  # once per distinct disagreement
                logger.warning(
                    "Gateway %s: active reserve 15508=%s disagrees with 16001=%s",
                    self._gateway_id, active, cross,
                )
                self._last_mismatch = pair
        elif self._last_mismatch is not None:
            self._last_mismatch = None
        apply_active_reserve(points, active)
