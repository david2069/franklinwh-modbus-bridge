"""SunSpec Explorer export: a gateway's models and current values (#228).

For sharing a device's SunSpec map (a GitHub issue, a Gist, another
integration's author). The serial number is redacted unless the user turns
that off; the gateway's address and name are never included, because nothing
here needs them.

Values are resolved the way the Explorer shows them (``explorer_tab.js``
``getPointValue``): the model-qualified key ``"<model>.<point>"`` first, then
the bare point name, then its snake_case form.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from franklinwh_bridge.support.scrub import SERIAL, compile_known, scrub_serial_patterns

#: Point names that are the serial number whatever their value looks like.
SERIAL_POINTS = frozenset({"SN"})

CSV_HEADER = ["Model", "Model label", "Point", "Label", "Value", "Type", "Unit",
              "Address (base-0)", "Address (base-1)", "Access", "Scale factor"]

#: How the aGate addresses its SunSpec map (#273; franklinwh-modbus QUIRKS,
#: "Register Addressing", tested 2026-03-15).
ADDRESSING_NOTE = (
    "The aGate serves its SunSpec map at base 0: address_base0 is the Modbus PDU "
    "address you send. address_base1 is the same register numbered from 1, as "
    "FranklinWH's PICS lists it. The conventional 40001+ (base 40000) addresses "
    "are not answered by the aGate: they return exception 2, illegal data address."
)


def _snake(name: str) -> str:
    return re.sub(r"([A-Z])", r"_\1", name).lower().lstrip("_")


def resolve_value(points: Mapping[str, Any], model_id: int, name: str) -> Any:
    """The value the Explorer shows for ``model_id``/``name`` (None if absent)."""
    for key in (f"{model_id}.{name}", name, _snake(name)):
        if key in points:
            return points[key]
    return None


def known_serials(values: Iterable[Any]) -> dict[str, str]:
    """Each serial on file, and its last 8 characters (the short id), → ``<serial>``."""
    known: dict[str, str] = {}
    for v in values:
        s = str(v or "").strip()
        if len(s) < 3:
            continue
        known[s] = SERIAL
        if len(s) > 8:
            known[s[-8:]] = SERIAL
    return known


class _Redactor:
    def __init__(self, serials: Mapping[str, str]) -> None:
        self._known = compile_known(serials)
        self.hits = 0

    def text(self, value: str) -> str:
        out = value
        for pat, placeholder in self._known:
            out = pat.sub(placeholder, out)
        out = scrub_serial_patterns(out)
        if out != value:
            self.hits += 1
        return out

    def point(self, name: str, value: Any) -> Any:
        if name in SERIAL_POINTS and value not in (None, ""):
            self.hits += 1
            return SERIAL
        return self.text(value) if isinstance(value, str) else value


def build_export(
    catalog: list[dict],
    points: Mapping[str, Any],
    *,
    model_ids: Iterable[int] | None = None,
    redact_serial: bool = True,
    serials: Iterable[Any] = (),
    gateway: Mapping[str, Any] | None = None,
    version: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """The export as a dict (the JSON form). ``model_ids`` None = every model.

    Raises ``KeyError`` naming any requested model the catalog doesn't have.
    """
    models: dict[int, dict] = {}
    for rec in catalog:
        mid = rec["model_id"]
        models.setdefault(mid, {"model_id": mid, "label": rec.get("model_label"), "recs": []})
        models[mid]["recs"].append(rec)

    wanted = sorted(models) if model_ids is None else list(dict.fromkeys(model_ids))
    missing = [m for m in wanted if m not in models]
    if missing:
        raise KeyError(", ".join(str(m) for m in missing))

    red = _Redactor(known_serials(serials)) if redact_serial else None
    out_models = []
    for mid in wanted:
        m = models[mid]
        pts = []
        recs = sorted(m["recs"], key=lambda r: (r.get("address") is None, r.get("address") or 0))
        for rec in recs:
            name = rec["point_name"]
            value = resolve_value(points, mid, name)
            if red is not None:
                value = red.point(name, value)
            pts.append({
                "name": name,
                "label": rec.get("label") or "",
                "value": value,
                "type": rec.get("type") or "",
                "unit": rec.get("unit") or "",
                "address_base0": rec.get("address"),
                "address_base1": None if rec.get("address") is None else rec["address"] + 1,
                "access": rec.get("access") or "R",
                "scale_factor": rec.get("scale_factor"),
            })
        out_models.append({"model_id": mid, "label": m["label"], "points": pts})

    gw = {}
    for key in ("device_type", "model", "firmware"):
        v = (gateway or {}).get(key)
        if v is not None:
            gw[key] = red.text(v) if (red is not None and isinstance(v, str)) else v

    return {
        "export": "sunspec",
        "bridge_version": version,
        "generated_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gateway": gw,
        "serial_redacted": redact_serial,
        "addressing": ADDRESSING_NOTE,
        "model_count": len(out_models),
        "point_count": sum(len(m["points"]) for m in out_models),
        "models": out_models,
    }


def render_json(export: Mapping[str, Any]) -> str:
    return json.dumps(export, indent=2, default=str) + "\n"


def render_csv(export: Mapping[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for m in export["models"]:
        for p in m["points"]:
            w.writerow([
                m["model_id"], m["label"] or "", p["name"], p["label"],
                "" if p["value"] is None else p["value"], p["type"], p["unit"],
                "" if p["address_base0"] is None else p["address_base0"],
                "" if p["address_base1"] is None else p["address_base1"], p["access"],
                "" if p["scale_factor"] is None else p["scale_factor"],
            ])
    return buf.getvalue()


def filename(export: Mapping[str, Any], fmt: str, all_models: bool) -> str:
    ids = [str(m["model_id"]) for m in export["models"]]
    scope = "all" if all_models else ("model_" if len(ids) == 1 else "models_") + "_".join(ids)
    suffix = "_redacted" if export["serial_redacted"] else ""
    return f"sunspec_{scope}{suffix}.{fmt}"
