"""Export the app log (#320): the Logs tab's current filter, as text or JSON.

Serials and IP addresses are masked, as the SunSpec export masks the serial:
first the exact serials and gateway hosts on file (and each serial's last 8
characters, the short id in MQTT topics), then the serial and IP patterns for
anything not on file. Loopback addresses stay, since they say "misconfigured",
not "where you live". Nothing else is changed, so the export reads like the tab.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from typing import Any

from franklinwh_bridge.support.scrub import (
    HOST,
    IP,
    compile_known,
    scrub_ip_patterns,
    scrub_serial_patterns,
)
from franklinwh_bridge.support.sunspec_export import known_serials

#: Export schema version: bump when a field changes.
SCHEMA_VERSION = 1
#: Most lines in one export; the filter narrows it further.
MAX_LINES = 100_000


def known_identifiers(gateways: Iterable[Mapping[str, Any]],
                      registry: Any = None) -> dict[str, str]:
    """Every serial and gateway host on file → its placeholder."""
    serials: list[Any] = []
    known: dict[str, str] = {}
    for g in gateways:
        if g.get("mock"):
            # A demo gateway's "mock" host and MOCK-DEMO serial identify no one;
            # masking them also hid every "mock" and "MOCK" in the log.
            continue
        serials.append(g.get("serial"))
        inst = registry.get(g.get("id")) if registry is not None else None
        if inst is not None:
            serials.append(getattr(inst.status, "serial", None))
        host = str(g.get("host") or "").strip()
        if len(host) >= 3:
            known[host] = IP if host.replace(".", "").isdigit() or ":" in host else HOST
    known.update(known_serials(serials))
    return known


class Redactor:
    """Masks serials and IPs in one message."""

    def __init__(self, known: Mapping[str, str]) -> None:
        self._known = compile_known(known)
        self.lines_changed = 0

    def __call__(self, text: str | None) -> str:
        raw = str(text or "")
        out = raw
        for pat, placeholder in self._known:
            out = pat.sub(placeholder, out)
        out = scrub_ip_patterns(scrub_serial_patterns(out))
        if out != raw:
            self.lines_changed += 1
        return out


def _local(ts: float) -> str:
    """The bridge's own time zone, as the Logs tab shows it."""
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def build(rows: Iterable[Mapping[str, Any]], known: Mapping[str, str], *,
          filters: Mapping[str, Any], version: str, total: int,
          now: float | None = None) -> dict[str, Any]:
    """``rows`` oldest first, as ``query_logs`` returns them reversed."""
    red = Redactor(known)
    lines = [
        {"ts": r["ts"], "time": _local(r["ts"]), "level": r.get("level") or "INFO",
         "source": r.get("name") or "", "gateway_id": r.get("gateway_id") or "",
         "message": red(r.get("message"))}
        for r in rows
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "bridge_version": version,
        "exported_at": _local(now if now is not None else dt.datetime.now().timestamp()),
        "filters": {k: v for k, v in filters.items() if v not in (None, "")},
        "count": len(lines),
        "matched": total,
        "masked": "serials and IP addresses",
        "lines_masked": red.lines_changed,
        "logs": lines,
    }


def render_json(export: Mapping[str, Any]) -> str:
    return json.dumps(export, indent=2, ensure_ascii=False)


def render_text(export: Mapping[str, Any]) -> str:
    """One line per entry, like the Logs tab, under a short header."""
    f = export["filters"]
    head = [
        f"# FranklinWH Modbus Bridge v{export['bridge_version']}: app log export",
        f"# Exported {export['exported_at']}",
        "# Filter: " + (", ".join(f"{k}={v}" for k, v in f.items()) or "none"),
        f"# {export['count']} line(s)"
        + (f" of {export['matched']} matching (newest kept)"
           if export["matched"] > export["count"] else ""),
        f"# Serials and IP addresses are masked ({export['lines_masked']} line(s) changed).",
        "",
    ]
    body = [
        f"{e['time']}  {e['level']:<8} {e['source']}"
        + (f" [{e['gateway_id']}]" if e["gateway_id"] else "")
        + f"  {e['message']}"
        for e in export["logs"]
    ]
    return "\n".join(head + body) + "\n"


def filename(fmt: str, now: float | None = None) -> str:
    stamp = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now().timestamp())
    return f"bridge_log_{stamp:%Y%m%d_%H%M}.{'txt' if fmt == 'text' else 'json'}"
