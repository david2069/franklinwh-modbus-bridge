"""SunSpec enums the bridge decodes itself, defined once.

Model 713 ``Sta`` is a battery HEALTH enum, per the SunSpec model 713
definition (``sunspec2/models/json/model_713.json``) and FranklinWH's PICS
(SM-000028): 0 OK, 1 WARNING, 2 ERROR. It is not an activity state;
charging/discharging comes from 714.DCW. The bridge used to decode it twice,
both wrongly: the poller with ERROR = 3, and the alarm history as an 8-value
activity enum (Idle/Charging/...). See docs/vendor-issues.md §4.
"""

from __future__ import annotations

M713_STA: dict[int, str] = {0: "OK", 1: "Warning", 2: "Error"}


def m713_sta_label(raw: int | None) -> str | None:
    """Health label for a raw 713.Sta, or "Unknown (n)" outside 0-2."""
    if raw is None:
        return None
    return M713_STA.get(int(raw), f"Unknown ({int(raw)})")


def m713_sta_severity(raw: int | None) -> str:
    """Alarm-history severity: ERROR is a fault; WARNING or an undefined
    value is a warning (an undefined value is itself worth a look)."""
    if raw is None or int(raw) == 0:
        return "info"
    return "fault" if int(raw) == 2 else "warning"
