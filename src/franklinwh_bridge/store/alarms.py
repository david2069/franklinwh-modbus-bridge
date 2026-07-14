"""Alarm event store — tracks SunSpec alarm/event register changes.

Alarm state is read every poll cycle (M701.Alrm, M714.PrtAlrms, M713.Sta).
This module only writes a row to ``alarm_events`` when the value changes,
keeping the table sparse.  Each row records the bits that transitioned set
and cleared so the chart can render a meaningful label without re-joining.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# ── Bit-name tables ────────────────────────────────────────────────────────

# Model 701 Alrm — standard SunSpec DER AC alarms (bits 0-16 standard per the
# official SunSpec Model 701 spec, confirmed against FranklinWH's own PICS
# certification, which marks all 17 as "supported"; bits 17-31 are vendor-
# defined and unconfirmed). CORRECTED 2026-07-13 — the previous table here
# was off-by-one from bit 1 onward and included three names that don't exist
# in the spec at all (InputOverCurrent, ArcFault, ThermalDerate). See
# docs/vendor-issues.md Issue 12a.
_M701_ALRM_BITS: dict[int, str] = {
    0:  "GroundFault",
    1:  "DCOverVoltage",
    2:  "ACDisconnect",
    3:  "DCDisconnect",
    4:  "GridDisconnect",
    5:  "CabinetOpen",
    6:  "ManualShutdown",
    7:  "OverTemp",
    8:  "OverFrequency",
    9:  "UnderFrequency",
    10: "ACOverVoltage",
    11: "ACUnderVoltage",
    12: "BlownStringFuse",
    13: "UnderTemp",
    14: "MemoryLoss",
    15: "HwTestFailure",
    16: "ManufacturerAlarm",
    # Bits 17-31 are vendor-defined; decode as VendorBit{n} if set
}

# Model 714 PrtAlrms — DC port / battery alarms
_M714_ALRM_BITS: dict[int, str] = {
    0: "PortOverVoltage",
    1: "PortUnderVoltage",
    2: "PortOverCurrent",
    3: "PortOverTemp",
    4: "PortUnderTemp",
    5: "ContactorFault",
    6: "FuseFault",
    7: "PortGroundFault",
}

# Model 713 Sta — battery operational state (enum, not bitfield)
_M713_STA_NAMES: dict[int, str] = {
    0: "Idle",
    1: "Charging",
    2: "Discharging",
    3: "Holding",
    4: "Full",
    5: "Empty",
    6: "FAULT",
    7: "Sleep",
}

# Severity rules per source — same semantic choice as before (ground fault,
# cabinet open, manual shutdown, blown fuse are fault-worthy; everything else
# warning-tier), re-mapped to the corrected bit positions above. The three
# newly-added bits (MemoryLoss, HwTestFailure, ManufacturerAlarm) default to
# warning pending vendor guidance on their real-world severity.
_FAULT_BITS_M701 = {0, 5, 6, 12}   # ground, cabinet, manual, blown string fuse
_WARN_BITS_M701 = {1, 2, 3, 4, 7, 8, 9, 10, 11, 13, 14, 15, 16}

_FAULT_BITS_M714 = {2, 5, 6, 7}
_WARN_BITS_M714 = {0, 1, 3, 4}

# SunSpec register/point metadata per alarm_events.source and the grid-mode
# pseudo-source used for PV-Clipping intervals — not stored in alarm_events
# itself, joined in at read time for display.
_SOURCE_META: dict[str, dict[str, Any]] = {
    "M701_Alrm":     {"register": 40076, "point": "DERMeasureAC.Alrm"},
    "M701_DERMode":  {"register": 40078, "point": "DERMeasureAC.DERMode"},
    "M713_Sta":      {"register": 41039, "point": "DERStorageCapacity.Sta"},
    "M714_PrtAlrms": {"register": 41044, "point": "DERMeasureDC.PrtAlrms"},
}

# grid_mode values worth surfacing as a distinct interval (the default
# "Grid Following" state is not an event).
_GRID_MODES_OF_INTEREST = {"PV Clipped", "Grid Forming"}


def _decode_bitfield(value: int, names: dict[int, str]) -> list[str]:
    """Return names of all set bits in *value*."""
    result = []
    for bit, name in names.items():
        if value & (1 << bit):
            result.append(name)
    # Any set bits beyond the known table
    known_bits = set(names.keys())
    for bit in range(max(known_bits) + 1 if names else 32, 32):
        if value & (1 << bit):
            result.append(f"VendorBit{bit}")
    return result


def _severity_bitfield(
    set_bits: list[str], fault_names: set[int], warn_names: set[int]
) -> str:
    """Return 'fault' | 'warning' | 'info' based on which bits are set."""
    # Look up bit numbers for the active names
    if any(
        bit in fault_names
        for bit, name in _M701_ALRM_BITS.items()
        if name in set_bits
    ):
        return "fault"
    if any(
        bit in warn_names
        for bit, name in _M701_ALRM_BITS.items()
        if name in set_bits
    ):
        return "warning"
    return "info"


def _severity_m701(set_bits: list[str]) -> str:
    for bit, name in _M701_ALRM_BITS.items():
        if name in set_bits:
            if bit in _FAULT_BITS_M701:
                return "fault"
    for bit, name in _M701_ALRM_BITS.items():
        if name in set_bits:
            if bit in _WARN_BITS_M701:
                return "warning"
    return "info"


def _severity_m714(set_bits: list[str]) -> str:
    for bit, name in _M714_ALRM_BITS.items():
        if name in set_bits:
            if bit in _FAULT_BITS_M714:
                return "fault"
    for bit, name in _M714_ALRM_BITS.items():
        if name in set_bits:
            if bit in _WARN_BITS_M714:
                return "warning"
    return "info"


def _name_severity(source: str, name: str, fallback: str) -> str:
    """Per-name severity, independent of whatever else co-occurred in the
    same raw-register transition.

    ``AlarmTracker.process_sample()`` computes one blanket severity per
    transition (worst bit wins) for logging purposes — correct for "was this
    transition dangerous," but wrong to apply to every bit name individually:
    a co-occurring fault-tier bit (e.g. ``ContactorFault``) would otherwise
    make an unrelated warning-tier bit (e.g. ``PortOverVoltage``) or a
    totally unclassified ``VendorBit*`` display as if it were itself
    fault-severity, when we have no actual information suggesting that.
    """
    if source == "M701_Alrm":
        bit_names, fault_bits, warn_bits = _M701_ALRM_BITS, _FAULT_BITS_M701, _WARN_BITS_M701
    elif source == "M714_PrtAlrms":
        bit_names, fault_bits, warn_bits = _M714_ALRM_BITS, _FAULT_BITS_M714, _WARN_BITS_M714
    else:
        # M713_Sta (state enum) and anything else: no bit-based severity
        # table exists here — use the transition's own recorded severity.
        return fallback
    for bit, bit_name in bit_names.items():
        if bit_name == name:
            if bit in fault_bits:
                return "fault"
            if bit in warn_bits:
                return "warning"
            return "info"
    # Unnamed/VendorBit*: genuinely unknown meaning — don't borrow a
    # neighbor's severity just because it happened to be set in the same
    # transition.
    return "info"


# ── Per-gateway state tracker (in-memory) ─────────────────────────────────────

class AlarmTracker:
    """Tracks previous alarm register values per gateway and writes change events."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        # {gateway_id: {source: last_raw_value}}
        self._prev: dict[str, dict[str, int]] = {}

    async def process_sample(self, points: dict[str, Any], gateway_id: str) -> None:
        """Check alarm fields in *points*, write event rows for any changes."""
        prev_gw = self._prev.setdefault(gateway_id, {})

        checks = [
            ("M701_Alrm",    points.get("system_alrm"),   _M701_ALRM_BITS, _severity_m701),
            ("M714_PrtAlrms", points.get("dc_port_alrm"), _M714_ALRM_BITS, _severity_m714),
        ]

        for source, raw, bit_names, sev_fn in checks:
            if raw is None:
                continue
            raw = int(raw)
            prev = prev_gw.get(source, -1)  # -1 = never seen
            if raw == prev:
                continue
            prev_gw[source] = raw

            if prev == -1:
                # First reading — only write if non-zero (actual alarm active)
                if raw == 0:
                    continue
                prev = 0  # treat "was clear" on first non-zero read

            prev_bits = _decode_bitfield(prev, bit_names)
            curr_bits = _decode_bitfield(raw, bit_names)
            set_bits = [b for b in curr_bits if b not in prev_bits]
            cleared_bits = [b for b in prev_bits if b not in curr_bits]

            if not set_bits and not cleared_bits:
                continue

            severity = sev_fn(set_bits) if set_bits else "info"
            alarms_set = ", ".join(set_bits)
            alarms_cleared = ", ".join(cleared_bits)

            label_parts = []
            if set_bits:
                label_parts.append(f"SET: {alarms_set}")
            if cleared_bits:
                label_parts.append(f"CLR: {alarms_cleared}")
            detail = f"{source} 0x{raw:08X} — {' | '.join(label_parts)}"
            logger.info("Alarm change [%s] %s: %s", gateway_id, source, detail)

            try:
                await self._db.execute(
                    "INSERT INTO alarm_events "
                    "(ts, gateway_id, source, value_raw, alarms_set, alarms_cleared, severity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), gateway_id, source, raw,
                     alarms_set, alarms_cleared, severity),
                )
                await self._db.commit()
            except Exception as exc:
                logger.warning("Failed to write alarm event: %s", exc)

        # M713 Sta — enum (not bitfield), track state changes
        sta_raw = points.get("battery_sta")
        if sta_raw is not None:
            sta_raw = int(sta_raw)
            prev_sta = prev_gw.get("M713_Sta", -1)
            if sta_raw != prev_sta:
                prev_gw["M713_Sta"] = sta_raw
                if prev_sta != -1:
                    prev_name = _M713_STA_NAMES.get(prev_sta, f"State{prev_sta}")
                    curr_name = _M713_STA_NAMES.get(sta_raw, f"State{sta_raw}")
                    severity = "fault" if sta_raw == 6 else "info"
                    detail = f"{prev_name} → {curr_name}"
                    logger.info("Battery state change [%s]: %s", gateway_id, detail)
                    try:
                        await self._db.execute(
                            "INSERT INTO alarm_events "
                            "(ts, gateway_id, source, value_raw, alarms_set, alarms_cleared, severity) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (time.time(), gateway_id, "M713_Sta", sta_raw,
                             curr_name, prev_name, severity),
                        )
                        await self._db.commit()
                    except Exception as exc:
                        logger.warning("Failed to write battery state event: %s", exc)


async def query_alarm_events(
    db: aiosqlite.Connection,
    start_ts: float,
    end_ts: float,
    gateway_id: str | None = None,
) -> list[dict]:
    """Return alarm events in [start_ts, end_ts], optionally filtered by gateway."""
    gw_filter = " AND gateway_id = ?" if gateway_id else ""
    gw_params: tuple = (gateway_id,) if gateway_id else ()

    events: list[dict] = []
    async with db.execute(
        f"SELECT ts, gateway_id, source, value_raw, alarms_set, alarms_cleared, severity "
        f"FROM alarm_events WHERE ts >= ? AND ts <= ?{gw_filter} ORDER BY ts",
        (start_ts, end_ts) + gw_params,
    ) as cur:
        async for row in cur:
            events.append({
                "ts": row[0],
                "gateway_id": row[1],
                "source": row[2],
                "value_raw": row[3],
                "alarms_set": row[4],
                "alarms_cleared": row[5],
                "severity": row[6],
            })
    return events


def pair_alarm_events(events: list[dict]) -> list[dict]:
    """Pair SET/open transitions with their later CLR/close transition.

    ``alarm_events`` rows are discrete transitions (see ``process_sample``
    above) — this walks them chronologically per ``(gateway_id, source)``
    and turns each open/close pair into one durationed row. This also
    handles ``M713_Sta`` state changes correctly with no special-casing:
    its ``alarms_cleared`` field is the *previous* state name, so "closing"
    that entry is exactly "the gateway left that state".

    Names still open at the end of *events* (no matching CLR yet) are
    returned with ``ongoing=True`` and a duration measured up to the last
    timestamp seen for that source.
    """
    # open[(gateway_id, source)][name] = {"ts": set_ts, "value_raw": ..., "severity": ...}
    open_entries: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    last_ts_per_key: dict[tuple[str, str], float] = {}
    paired: list[dict] = []

    for ev in sorted(events, key=lambda e: e["ts"]):
        key = (ev["gateway_id"], ev["source"])
        last_ts_per_key[key] = ev["ts"]
        open_for_key = open_entries.setdefault(key, {})
        meta = _SOURCE_META.get(ev["source"], {})

        set_names = [n.strip() for n in (ev["alarms_set"] or "").split(",") if n.strip()]
        cleared_names = [n.strip() for n in (ev["alarms_cleared"] or "").split(",") if n.strip()]

        for name in set_names:
            open_for_key[name] = {
                "ts": ev["ts"],
                "value_raw": ev["value_raw"],
                "severity": _name_severity(ev["source"], name, ev["severity"]),
            }

        for name in cleared_names:
            opened = open_for_key.pop(name, None)
            set_ts = opened["ts"] if opened else ev["ts"]
            severity = (
                opened["severity"] if opened
                else _name_severity(ev["source"], name, ev["severity"])
            )
            paired.append({
                "ts": set_ts,
                "end_ts": ev["ts"],
                "duration_seconds": max(0.0, ev["ts"] - set_ts),
                "ongoing": False,
                "gateway_id": ev["gateway_id"],
                "source": ev["source"],
                "register": meta.get("register"),
                "point": meta.get("point"),
                "name": name,
                "value_raw": opened["value_raw"] if opened else ev["value_raw"],
                "severity": severity,
            })

    # Anything still open has no CLR in this window — report as ongoing.
    for key, open_for_key in open_entries.items():
        gateway_id, source = key
        meta = _SOURCE_META.get(source, {})
        last_ts = last_ts_per_key.get(key)
        for name, opened in open_for_key.items():
            paired.append({
                "ts": opened["ts"],
                "end_ts": last_ts,
                "duration_seconds": max(0.0, (last_ts or opened["ts"]) - opened["ts"]),
                "ongoing": True,
                "gateway_id": gateway_id,
                "source": source,
                "register": meta.get("register"),
                "point": meta.get("point"),
                "name": name,
                "value_raw": opened["value_raw"],
                "severity": opened["severity"],
            })

    return paired


def compute_grid_mode_intervals(points: list[dict]) -> list[dict]:
    """Run-length-encode ``grid_mode`` samples into durationed intervals.

    Only modes in ``_GRID_MODES_OF_INTEREST`` (e.g. "PV Clipped") are
    emitted — the default "Grid Following" state is not an event. The
    ``end_ts`` of a closed interval is the timestamp of the *next*
    differing sample (when the mode actually changed away), not the last
    sample still in that mode — a single-sample blip must still report a
    duration of roughly one poll interval, not 0.
    """
    meta = _SOURCE_META.get("M701_DERMode", {})
    intervals: list[dict] = []
    prev_mode: str | None = None
    seg_start: float | None = None

    ordered = sorted((p for p in points if p.get("ts") is not None), key=lambda p: p["ts"])

    for point in ordered:
        mode = point.get("grid_mode")
        ts = point["ts"]
        if mode != prev_mode:
            if prev_mode in _GRID_MODES_OF_INTEREST and seg_start is not None:
                intervals.append({
                    "ts": seg_start,
                    "end_ts": ts,
                    "duration_seconds": max(0.0, ts - seg_start),
                    "ongoing": False,
                    "gateway_id": point.get("gateway_id", "default"),
                    "source": "M701_DERMode",
                    "register": meta.get("register"),
                    "point": meta.get("point"),
                    "name": prev_mode,
                    "value_raw": None,
                    "severity": "info",
                })
            seg_start = ts
            prev_mode = mode

    # Trailing open segment — still in a mode of interest at the last sample,
    # with no later differing sample in range to close it.
    if prev_mode in _GRID_MODES_OF_INTEREST and seg_start is not None and ordered:
        last_ts = ordered[-1]["ts"]
        intervals.append({
            "ts": seg_start,
            "end_ts": last_ts,
            "duration_seconds": max(0.0, last_ts - seg_start),
            "ongoing": True,
            "gateway_id": ordered[-1].get("gateway_id", "default"),
            "source": "M701_DERMode",
            "register": meta.get("register"),
            "point": meta.get("point"),
            "name": prev_mode,
            "value_raw": None,
            "severity": "info",
        })

    return intervals
