"""Which register gave this poll's home load: 15506 or 16000.

franklinwh-modbus reads both and returns ``home_load_ext``: 16000 (~1 W, the
library's high-resolution home load) when it reads above 0, or 15506
(``LoadActiveP``, 100 W steps) otherwise. The bridge uses that value as
returned. The register is recorded on every sample as ``home_load_source``
for support and the diagnostic entity; the per-gateway choice (#35) is gone.
"""

from __future__ import annotations

from typing import Any


def record_source(points: dict[str, Any]) -> None:
    """Set ``home_load_source`` from the library's rule, using the 16000 read."""
    std = points.get("home_load_ext_quantized")
    if std is None:
        return  # extension read failed this poll
    hires = points.get("vreg_16000")
    used_hires = hires is not None and (hires > 0 or std == 0)
    points["home_load_source"] = "16000" if used_hires else "15506"
