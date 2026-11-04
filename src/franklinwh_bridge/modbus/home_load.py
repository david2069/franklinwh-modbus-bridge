"""Home load register choice: standard 15506 or high-res 16000 (#35).

15506 (``LoadActiveP``) is documented and quantised to 100 W. 16000 is an
undocumented mirror at ~1 W (franklinwh-modbus FRANKLINWH_SUNSPEC_QUIRKS.md,
16000 correlation table). franklinwh-modbus silently prefers 16000 whenever it
reads > 0. It tracks load on the reference site. An unverified community report
(franklinwh-modbus#18) claims it sat at 1000 on another site while the load
moved — a lead, not evidence, but enough reason not to trust 16000 blindly.

One site is not sufficient to say 16000 is right everywhere, so the
source is a per-gateway choice, recorded on every sample as
``home_load_source`` for support, and high-res is only used while it agrees with
15506. On the reference site 15506 looked like a floor to the 100 W step
(1004 → 1000, 835 → 800; four readings, one site), so the check accepts 16000
within [15506 − 50, 15506 + 150]: wide enough for floor or round and for the two
registers being read a moment apart.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STANDARD = "standard"
HIGH_RES = "high_res"
SOURCES = (STANDARD, HIGH_RES)

_BELOW_W = 50  # 16000 may sit this far under 15506 …
_ABOVE_W = 150  # … or this far over (100 W step + margin)


def plausible(hires: float, std: float) -> bool:
    """Whether a 16000 reading agrees with the quantised 15506 reading."""
    return std - _BELOW_W <= hires <= std + _ABOVE_W


class HomeLoadSelector:
    """Set ``home_load_ext`` from the configured register, with a sanity check."""

    def __init__(self, gateway_id: str) -> None:
        self._gateway_id = gateway_id
        self._falling_back = False

    def apply(self, points: dict[str, Any], source: str) -> None:
        std = points.get("home_load_ext_quantized")
        hires = points.get("vreg_16000")
        if std is None:
            return  # extension read failed this poll: leave the library's value
        if source == HIGH_RES and hires is not None:
            if plausible(hires, std):
                if self._falling_back:
                    logger.info(
                        "Gateway %s: home load 16000 agrees with 15506 again — "
                        "using high-res", self._gateway_id,
                    )
                    self._falling_back = False
                points["home_load_ext"] = hires
                points["home_load_source"] = "16000"
                return
            if not self._falling_back:
                logger.warning(
                    "Gateway %s: home load 16000=%sW disagrees with 15506=%sW — "
                    "using 15506 until they agree", self._gateway_id, hires, std,
                )
                self._falling_back = True
            points["home_load_ext"] = std
            points["home_load_source"] = "15506 (16000 implausible)"
            return
        points["home_load_ext"] = std
        points["home_load_source"] = "15506"
