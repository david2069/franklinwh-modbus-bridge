"""Per-phase utilisation detection from SunSpec 701 registers.

A gateway's wired phase(s) can be inferred from Model 701 (DER AC Measurement)
per-phase points without any extra Modbus traffic — the poller already caches
them as ``701.<point>`` keys:

- ``701.VL1/VL2/VL3`` (phase-N voltage) — best *presence* signal: a connected
  phase reads ~230-240 V (scaled), an unused phase reads 0. Present even at
  zero power, so it does not flicker.
- ``701.TotWhInjLn`` + ``701.TotWhAbsLn`` (lifetime energy) — best *utilisation*
  signal: monotonic counters, immune to transient nulls.

Heuristic: a phase is **connected** if ``VLn > 0`` and **utilised** if the sum
of its lifetime energy counters is > 0. The detected wiring is the set of
connected (or utilised) phases — e.g. only L1 ⇒ single-phase on L1.
"""

from __future__ import annotations

from typing import Any

PHASES = ("L1", "L2", "L3")


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def detect_phases(points: dict[str, Any]) -> dict[str, Any]:
    """Infer connected/utilised phases from a gateway's latest 701 points.

    Returns a dict with a per-phase breakdown plus a ``detected`` summary
    (e.g. ``"L1"``, ``"L1+L2"``, ``"L1+L2+L3"``, or ``None`` if no 701 data).
    """
    per_phase: dict[str, dict[str, Any]] = {}
    connected_phases: list[str] = []
    any_data = False

    for ph in PHASES:
        v = points.get(f"701.V{ph}")
        inj = points.get(f"701.TotWhInj{ph}")
        absn = points.get(f"701.TotWhAbs{ph}")
        if v is not None or inj is not None or absn is not None:
            any_data = True
        energy = int(_num(inj) + _num(absn))
        connected = _num(v) > 0
        utilised = energy > 0
        per_phase[ph] = {
            "connected": connected,
            "utilised": utilised,
            "voltage_raw": v,
            "energy_wh": energy,
        }
        if connected or utilised:
            connected_phases.append(ph)

    return {
        "phases": per_phase,
        "detected": "+".join(connected_phases) if connected_phases else None,
        "has_data": any_data,
    }


def phase_matches(declared: str | None, detected: str | None) -> bool:
    """True when a user's declared ``phase`` is consistent with detection.

    ``'all'`` (or empty) always matches — it asserts no specific phase.
    Otherwise compare the sorted leg sets so ``'L2+L1'`` == ``'L1+L2'``.
    Unknown detection (``None``) is treated as a match (nothing to contradict).
    """
    if not declared or declared == "all":
        return True
    if not detected:
        return True

    def norm(s: str) -> tuple[str, ...]:
        return tuple(sorted(p.strip() for p in s.split("+") if p.strip()))

    return norm(declared) == norm(detected)
