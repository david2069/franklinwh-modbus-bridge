"""Build the redacted support bundle (``GET /api/support/info``).

Aligned with the Local Bridge's "Support info" bundle: models, firmware,
counts, modes and settings — never serials, hosts, IPs, location or
credentials.

Structured fields come from an **allowlist**: each value below is picked on
purpose, so a new column on a table cannot leak into the bundle by accident.
The only free text is log messages and last-error strings, and those go
through :mod:`franklinwh_bridge.support.scrub` with every identifying value
this install has on file.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib.metadata
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from franklinwh_bridge import __version__
from franklinwh_bridge.support.scrub import (
    COORD,
    EMAIL,
    HOST,
    IP,
    SERIAL,
    TOKEN,
    URL,
    compile_known,
    scrub,
)

logger = logging.getLogger(__name__)

#: First-run date, set once and never overwritten (as the Local Bridge does).
INSTALL_DATE_KEY = "install_date"

#: Libraries whose versions matter when reading a report.
LIBRARIES = ("franklinwh-modbus", "pysunspec2", "pymodbus", "fastapi", "aiomqtt", "aiosqlite")

#: How far back the "recent problems" section looks, and how many lines it shows.
PROBLEM_WINDOW_S = 24 * 3600
PROBLEM_LINES = 20
#: Longest message shown; a traceback or a banner is cut, not pasted whole.
MESSAGE_MAX = 400

#: Marks the startup disclaimer banner (see ``disclaimer.banner``).
_BANNER_MARK = "UNOFFICIAL SOFTWARE — PLEASE READ"

AC_TYPES = {0: "single phase", 1: "split phase", 2: "three phase"}

#: Names and ids that are generic, not identifying. Replacing them would turn
#: "Connected to aGate" into nonsense, so they are left in log text.
_GENERIC = frozenset(
    {
        "agate",
        "agate (mock)",
        "mock",
        "default",
        "gateway",
        "demo",
        "my site",
        "site",
        "admin",
        "user",
        "viewer",
        "api",
        "test",
        "home",
        "home assistant",
        "local",
        "franklinwh",
        "mqtt",
        "broker",
        "localhost",
        "mac1",
        "mac-1",
    }
)

#: What the header says was removed. Kept next to the code that removes it.
REDACTED_FIELDS = (
    "serials (full and short ids)",
    "IP addresses, host names and ports",
    "MAC addresses",
    "location (latitude/longitude)",
    "usernames and emails",
    "Home Assistant URLs and tokens",
    "passwords and MQTT credentials",
    "the MQTT client id",
    "gateway, site and device names",
    "session ids",
    "home-directory paths",
)


# ── install date ─────────────────────────────────────────────────────────────


async def ensure_install_date(db: aiosqlite.Connection) -> str | None:
    """Record the first-run date once (YYYY-MM-DD, UTC); never overwrite it.

    An install that predates this key gets the date its database was created
    (the first migration's ``applied_at``) rather than today's, which would
    claim every upgraded install began on the day it was upgraded.
    """
    from franklinwh_bridge.store.db import get_app_config, set_app_config

    current = await get_app_config(db, INSTALL_DATE_KEY, None)
    if current:
        return current
    first: float | None = None
    with contextlib.suppress(Exception):
        async with db.execute("SELECT MIN(applied_at) FROM schema_version") as cur:
            row = await cur.fetchone()
            first = row[0] if row else None
    when = dt.datetime.fromtimestamp(first or time.time(), dt.UTC).strftime("%Y-%m-%d")
    await set_app_config(db, INSTALL_DATE_KEY, when)
    return when


# ── helpers ──────────────────────────────────────────────────────────────────


def _lib_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in LIBRARIES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def _git_commit() -> str | None:
    """The source commit, when it can be known: ``GIT_COMMIT`` (or
    ``SOURCE_COMMIT``) from the build, else ``git`` in a source checkout."""
    for var in ("GIT_COMMIT", "SOURCE_COMMIT"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v[:12]
    here = Path(__file__).resolve()
    if not any((p / ".git").exists() for p in here.parents):
        return None
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
            cwd=str(here.parent),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def _utc_offset() -> str:
    off = time.localtime().tm_gmtoff
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"UTC{sign}{off // 3600:02d}:{off % 3600 // 60:02d}"


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _add_known(known: dict[str, str], value: Any, placeholder: str) -> None:
    v = str(value).strip() if value is not None else ""
    if len(v) >= 3 and v.lower() not in _GENERIC:
        known.setdefault(v, placeholder)


def _host_placeholder(host: str) -> str:
    return IP if host.replace(".", "").isdigit() or ":" in host else HOST


async def _rows(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        async with db.execute(sql, params) as cur:
            names = [d[0] for d in cur.description]
            return [dict(zip(names, tuple(r), strict=False)) for r in await cur.fetchall()]
    except Exception:  # noqa: BLE001 — a missing table must not sink the bundle
        return []


# ── the bundle ───────────────────────────────────────────────────────────────


async def build_support_info(state: Any, *, now: float | None = None) -> dict[str, Any]:
    """Collect the redacted bundle from the running app's ``state``."""
    from franklinwh_bridge.api import health
    from franklinwh_bridge.config import clock
    from franklinwh_bridge.config.supervisor import is_supervisor_instance
    from franklinwh_bridge.gateway.dynamic_pricing import is_dynamic
    from franklinwh_bridge.gateway.modules import get_modules
    from franklinwh_bridge.store import link_stats as link_stats_store
    from franklinwh_bridge.store import point_history
    from franklinwh_bridge.store.db import (
        get_app_config,
        get_gateways,
        get_ha_instances,
        get_mqtt_config,
        get_schedules,
        get_schema_version,
        get_services,
        get_site_config,
        get_users,
    )

    now = now or time.time()
    db: aiosqlite.Connection = state.db
    config = getattr(state, "config", None)
    environment = str(getattr(config, "environment", "unknown"))
    registry = getattr(state, "registry", None)
    publisher = getattr(state, "mqtt_publisher", None)
    ha_registry = getattr(state, "ha_registry", None)

    gw_rows = await get_gateways(db)
    mqtt_cfg = await get_mqtt_config(db)
    ha_rows = await get_ha_instances(db)
    users = await get_users(db)
    site = await get_site_config(db)
    services = await get_services(db)

    # Every identifying value on file → its placeholder, for the log scrub.
    known: dict[str, str] = {}
    labels: dict[str, str] = {}
    for i, g in enumerate(gw_rows, start=1):
        label = f"gateway {i}"
        labels[g["id"]] = label
        host = (g.get("host") or "").strip()
        if host:
            _add_known(known, host, _host_placeholder(host))
        serials = [g.get("serial")]
        inst = registry.get(g["id"]) if registry else None
        if inst is not None:
            serials.append(getattr(inst.status, "serial", None))
        for sn in filter(None, serials):
            _add_known(known, sn, SERIAL)
            if len(str(sn)) > 8:
                _add_known(known, str(sn)[-8:], SERIAL)
        _add_known(known, g.get("name"), f"<{label}>")
        _add_known(known, g.get("id"), f"<{label}>")
    for key in ("host", "username", "password", "client_id"):
        placeholder = {"host": HOST, "client_id": "<client_id>"}.get(key, "<redacted>")
        _add_known(known, mqtt_cfg.get(key), placeholder)
    if publisher is not None:
        _add_known(known, getattr(publisher, "_host", None), HOST)
        _add_known(known, getattr(publisher, "_username", None), "<redacted>")
        _add_known(known, getattr(publisher, "_client_id", None), "<client_id>")
    for i, h in enumerate(ha_rows, start=1):
        url = (h.get("base_url") or "").strip()
        _add_known(known, url, URL)
        with contextlib.suppress(ValueError):
            hn = urlsplit(url).hostname
            if hn:
                _add_known(known, hn, _host_placeholder(hn))
        _add_known(known, h.get("token"), TOKEN)
        _add_known(known, h.get("name"), f"<ha {i}>")
    for u in users:
        uname = u.get("username")
        _add_known(known, uname, EMAIL if "@" in str(uname or "") else "<user>")
    for k in ("name", "meter_number", "account_number", "description"):
        _add_known(known, site.get(k), "<site>")
    for s in services:
        _add_known(known, s.get("name"), "<service>")
    solar = getattr(state, "solar_forecast", None)
    pv = getattr(solar, "settings", None)
    for coord in (getattr(pv, "pv_latitude", None), getattr(pv, "pv_longitude", None)):
        if coord is not None:
            _add_known(known, coord, COORD)

    def clean(text: Any) -> str | None:
        return scrub(str(text), known) if text else None

    def known_only(text: Any) -> str | None:
        """Structured values (model, firmware): only exact known values go."""
        if not text:
            return None
        out = str(text)
        for pat, placeholder in compile_known(known):
            out = pat.sub(placeholder, out)
        return out

    # ── bridge ──
    try:
        tz = await clock.status(db)
    except Exception:  # noqa: BLE001
        tz = {}
    bridge = {
        "version": __version__,
        "git_commit": await asyncio.to_thread(_git_commit),
        "environment": environment,
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "libraries": _lib_versions(),
        "schema_version": await get_schema_version(db),
        "install_date": await get_app_config(db, INSTALL_DATE_KEY, None),
        "uptime_s": round(now - health._start_time),
        # The Local Bridge's bundle carries no zone name; the offset is enough
        # to read the log times, and the zone narrows down where you live.
        "utc_offset": _utc_offset(),
        "timezone_confirmed": bool(tz.get("confirmed")),
        "timezone_matches_expected": tz.get("matches_expected"),
    }

    # ── gateways ──
    since = (now // 3600 - 23) * 3600
    gateways = []
    for g in gw_rows:
        inst = registry.get(g["id"]) if registry else None
        poller = getattr(inst, "poller", None) if inst else None
        status = getattr(inst, "status", None) if inst else None
        pstate = getattr(poller, "state", None)
        ac_type = getattr(status, "ac_type", None) if status else None
        if ac_type is None:
            ac_type = g.get("ac_type")
        last_poll = getattr(pstate, "last_poll_ts", None) if pstate else None
        row: dict[str, Any] = {
            "label": labels[g["id"]],
            "mock": bool(g.get("mock")),
            "enabled": bool(g.get("enabled")),
            "configured": bool((g.get("host") or "").strip()) or bool(g.get("mock")),
            "device_type": g.get("device_type") or "agate",
            "model": known_only(getattr(status, "model", None) or g.get("model")),
            "firmware": known_only(getattr(status, "firmware", None) or g.get("firmware")),
            "ac_type": AC_TYPES.get(ac_type, ac_type),
            "phase": g.get("phase"),
            "battery_ports": getattr(poller, "battery_port_count", None),
            "publish_to_ha": bool(g.get("publish_to_ha", 1)),
            "home_load_source": g.get("home_load_source"),
            "poll_interval_s": g.get("poll_interval"),
            "running": inst is not None,
            "health": getattr(status, "health", None),
            "connected": bool(getattr(status, "connected", False)),
            "polling": bool(getattr(status, "polling", False)),
            "last_poll_age_s": round(now - last_poll, 1) if last_poll else None,
            "polls": getattr(pstate, "polls_total", None),
            "poll_errors": getattr(pstate, "errors_total", None),
            "consecutive_errors": getattr(pstate, "consecutive_errors", None),
            "last_error": clean(getattr(status, "last_error", None)),
        }
        rows = await link_stats_store.query(db, g["id"], since)
        with contextlib.suppress(Exception):
            rows = rows + link_stats_store.open_hour(g["id"])
        if rows:
            req = [r for r in rows if r.get("kind") != "link"]
            link = [r for r in rows if r.get("kind") == "link"]

            def total(rs: list[dict], col: str) -> int:
                return int(sum(r.get(col) or 0 for r in rs))

            p95 = [r["lat_p95_ms"] for r in req if r.get("lat_p95_ms") is not None]
            row["link_stats_24h"] = {
                "requests": total(req, "count"),
                "ok": total(req, "ok"),
                "timeouts": total(req, "timeout_no_reply") + total(req, "timeout_closed"),
                "errors": total(req, "error"),
                "reconnects": total(link, "reconnects"),
                "reconnects_failed": total(link, "reconnects_failed"),
                "rescans": total(link, "rescans"),
                "worst_p95_ms": round(max(p95), 1) if p95 else None,
            }
        gateways.append(row)

    stats = getattr(state, "stats", None)
    totals = None
    if stats is not None:
        snap = stats.snapshot.to_dict()
        totals = {
            k: snap.get(k)
            for k in (
                "polls_total",
                "polls_ok",
                "polls_stale",
                "polls_error",
                "samples_rejected",
                "sanitizations",
                "conn_drops",
                "conn_recoveries",
                "mqtt_sent",
            )
        }

    # ── MQTT ──
    pub_state = getattr(publisher, "state", None)
    broker = getattr(state, "mqtt_broker", None) or {}
    sources = getattr(state, "mqtt_sources", None) or {}
    mqtt = {
        "enabled": bool(mqtt_cfg.get("enabled")),
        "connected": bool(getattr(pub_state, "connected", False)),
        "broker_source": sources.get("host") or broker.get("source"),
        "waiting_for_broker": bool(broker.get("waiting_for_broker")),
        "credentials_set": bool(mqtt_cfg.get("username")),
        "topic_prefix": (getattr(publisher, "topic_prefix", None) or mqtt_cfg.get("topic_prefix")),
        "discovery_prefix": mqtt_cfg.get("discovery_prefix"),
        "entity_count": len(publisher.entities) if publisher is not None else 0,
        "discovery_published": bool(getattr(pub_state, "discovery_published", False)),
        "messages_sent": getattr(pub_state, "messages_sent", None),
        "last_error": clean(getattr(pub_state, "last_error", None)),
    }

    # ── Home Assistant (inbound) ──
    connected = 0
    for h in ha_rows:
        inst = ha_registry.get(h["id"]) if ha_registry is not None else None
        if inst is not None and getattr(inst, "connected", False):
            connected += 1
    ha = {
        "instances": len(ha_rows),
        "enabled": sum(1 for h in ha_rows if h.get("enabled")),
        "connected": connected,
        "supervisor_managed": sum(1 for h in ha_rows if is_supervisor_instance(h)),
        "ingress": environment == "ha_addon",
    }

    # ── features ──
    modules = await get_modules(db)
    schedules = await get_schedules(db)
    ph = await point_history.get_config(db)
    nem = getattr(state, "nem_pricing", None)
    nem_settings = getattr(nem, "settings", None)
    features = {
        "modules_enabled": sorted(m["id"] for m in modules if m.get("enabled")),
        "schedules": len(schedules),
        "schedules_enabled": sum(1 for s in schedules if s.get("enabled")),
        "point_history": {
            "enabled": bool(ph.get("enabled")),
            "points": len(ph.get("points") or []),
            "interval_s": ph.get("interval_s"),
            "retention_days": ph.get("retention_days"),
        },
        "solar_forecast": {
            "configured": bool(getattr(pv, "configured", False)),
            # The array itself (like the Local Bridge) — never its location.
            "pv_kwp": getattr(pv, "pv_kwp", None),
            "pv_tilt": getattr(pv, "pv_tilt", None),
            "pv_azimuth": getattr(pv, "pv_azimuth", None),
        },
        "nem_region": getattr(nem_settings, "nem_region", "") or None,
        "ha_price_entity_set": bool(getattr(nem_settings, "tariff_price_entity", "")),
        "services": len(services),
        "dynamic_tariff": any(
            is_dynamic(s.get("pricing") if isinstance(s.get("pricing"), dict) else None)
            for s in services
        ),
    }
    roles: dict[str, int] = {}
    for u in users:
        if u.get("enabled", True):
            roles[u.get("role") or "?"] = roles.get(u.get("role") or "?", 0) + 1
    features["users_by_role"] = dict(sorted(roles.items()))

    # ── recent problems ──
    problems = await _recent_problems(db, getattr(state, "log_buffer", None), now, labels, clean)

    return {
        "note": header_note(),
        "generated_at": _iso(now),
        "bridge": bridge,
        "gateways": gateways,
        "bridge_totals": totals,
        "mqtt": mqtt,
        "home_assistant": ha,
        "features": features,
        "recent_problems": problems,
    }


async def _recent_problems(
    db: aiosqlite.Connection,
    buffer: Any,
    now: float,
    labels: dict[str, str],
    clean: Any,
) -> dict[str, Any]:
    since = now - PROBLEM_WINDOW_S
    rows = await _rows(
        db,
        "SELECT ts, level, name, message, gateway_id FROM logs "
        "WHERE ts >= ? AND level IN ('WARNING', 'ERROR', 'CRITICAL') ORDER BY ts",
        (since,),
    )
    # Not yet flushed to the database: the newest lines are still in memory.
    seen = {(r["ts"], r["name"], r["message"]) for r in rows}
    for e in list(buffer or []):
        if (
            e.get("ts", 0) >= since
            and e.get("level") in ("WARNING", "ERROR", "CRITICAL")
            and (e.get("ts"), e.get("name"), e.get("message")) not in seen
        ):
            rows.append(e)
    # The legal notice is logged as a WARNING at every start on purpose (so it
    # leads the log); it is not a problem, and it is 40 lines long.
    rows = [r for r in rows if _BANNER_MARK not in (r.get("message") or "")]
    rows.sort(key=lambda r: r.get("ts") or 0)

    by_source: dict[str, dict[str, int]] = {}
    for r in rows:
        src = clean(r.get("name")) or "?"
        lvl = r.get("level") or "?"
        by_source.setdefault(src, {})
        by_source[src][lvl] = by_source[src].get(lvl, 0) + 1
    recent = [
        {
            "time": _iso(r.get("ts")),
            "level": r.get("level"),
            "source": clean(r.get("name")),
            "gateway": labels.get(r.get("gateway_id") or ""),
            "message": _shorten(clean(r.get("message")) or ""),
        }
        for r in rows[-PROBLEM_LINES:]
    ]
    return {
        "window_hours": PROBLEM_WINDOW_S // 3600,
        "warnings": sum(1 for r in rows if r.get("level") == "WARNING"),
        "errors": sum(1 for r in rows if r.get("level") in ("ERROR", "CRITICAL")),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -sum(kv[1].values()))),
        "last": recent,
    }


def _shorten(msg: str) -> str:
    return msg if len(msg) <= MESSAGE_MAX else msg[: MESSAGE_MAX - 1] + "…"


def header_note() -> str:
    return (
        "Redacted support info. Removed: " + ", ".join(REDACTED_FIELDS) + ". "
        "Log messages are scrubbed by pattern, which can miss something — "
        "review before posting."
    )


# ── markdown ─────────────────────────────────────────────────────────────────


def _fmt(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or "—"
    return str(v)


def _duration(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def _kv(lines: list[str], items: list[tuple[str, Any]]) -> None:
    for k, v in items:
        lines.append(f"- {k}: {_fmt(v)}")


def render_markdown(info: dict[str, Any]) -> str:
    """The bundle as Markdown, ready to paste into a GitHub issue."""
    b = info["bridge"]
    L: list[str] = [
        "### Support info — FranklinWH Modbus Bridge (redacted)",
        "",
        f"> {info['note']}",
        "",
        f"Generated {info['generated_at']} (UTC)",
        "",
        "#### Bridge",
    ]
    libs = ", ".join(f"{k} {v or '—'}" for k, v in (b.get("libraries") or {}).items())
    _kv(
        L,
        [
            ("Version", b["version"] + (f" ({b['git_commit']})" if b.get("git_commit") else "")),
            ("Environment", b["environment"]),
            ("Python / OS", f"{b['python']} · {b['os']} · {b['arch']}"),
            ("Libraries", libs),
            ("DB schema", b["schema_version"]),
            ("Installed", b["install_date"]),
            ("Uptime", _duration(b["uptime_s"])),
            ("Clock", f"{b['utc_offset']} · timezone confirmed: {_fmt(b['timezone_confirmed'])}"),
        ],
    )
    L += ["", "#### Gateways"]
    if not info["gateways"]:
        L.append("- none")
    for g in info["gateways"]:
        tags = (
            [g["device_type"]]
            + (["mock"] if g["mock"] else [])
            + ([] if g["enabled"] else ["disabled"])
            + ([] if g["configured"] else ["not configured"])
        )
        L.append(
            f"- **{g['label']}** ({', '.join(tags)}): model {_fmt(g['model'])}, "
            f"firmware {_fmt(g['firmware'])}"
        )
        L.append(
            f"  - wiring {_fmt(g['ac_type'])}, phase {_fmt(g['phase'])}, "
            f"battery ports {_fmt(g['battery_ports'])}, home load {_fmt(g['home_load_source'])}, "
            f"poll {_fmt(g['poll_interval_s'])} s, publish to HA {_fmt(g['publish_to_ha'])}"
        )
        age = g["last_poll_age_s"]
        L.append(
            f"  - health {_fmt(g['health'])}, connected {_fmt(g['connected'])}, "
            f"polling {_fmt(g['polling'])}, last poll "
            f"{(str(age) + ' s ago') if age is not None else '—'}, "
            f"polls {_fmt(g['polls'])}, errors {_fmt(g['poll_errors'])} "
            f"(consecutive {_fmt(g['consecutive_errors'])})"
        )
        ls = g.get("link_stats_24h")
        if ls:
            L.append(
                f"  - link (24 h): {ls['requests']} requests, {ls['ok']} ok, "
                f"{ls['timeouts']} timeouts, {ls['errors']} errors, "
                f"{ls['reconnects']} reconnects ({ls['reconnects_failed']} failed), "
                f"{ls['rescans']} rescans, worst p95 {_fmt(ls['worst_p95_ms'])} ms"
            )
        if g.get("last_error"):
            L.append(f"  - last error: `{g['last_error'].replace('`', chr(39))}`")
    t = info.get("bridge_totals")
    if t:
        L.append(
            f"- all gateways: polls {t['polls_total']} (ok {t['polls_ok']}, stale "
            f"{t['polls_stale']}, error {t['polls_error']}), samples rejected "
            f"{t['samples_rejected']}, sanitized {t['sanitizations']}, connection drops "
            f"{t['conn_drops']} / recoveries {t['conn_recoveries']}"
        )
    m = info["mqtt"]
    L += ["", "#### MQTT"]
    _kv(
        L,
        [
            ("Enabled / connected", f"{_fmt(m['enabled'])} / {_fmt(m['connected'])}"),
            ("Broker from", m["broker_source"]),
            ("Namespace", f"{_fmt(m['topic_prefix'])} (discovery {_fmt(m['discovery_prefix'])})"),
            (
                "Entities",
                f"{m['entity_count']}, discovery published {_fmt(m['discovery_published'])}",
            ),
        ],
    )
    if m.get("last_error"):
        L.append(f"- Last error: `{m['last_error'].replace('`', chr(39))}`")
    h = info["home_assistant"]
    L += ["", "#### Home Assistant (inbound entities)"]
    _kv(
        L,
        [
            ("Instances", f"{h['instances']} ({h['enabled']} enabled, {h['connected']} connected)"),
            ("Ingress (add-on)", h["ingress"]),
        ],
    )
    f = info["features"]
    ph, so = f["point_history"], f["solar_forecast"]
    L += ["", "#### Features"]
    _kv(
        L,
        [
            ("Modules on", f["modules_enabled"]),
            ("Schedules", f"{f['schedules']} ({f['schedules_enabled']} enabled)"),
            (
                "Point history",
                f"{_fmt(ph['enabled'])}, {ph['points']} points, "
                f"every {ph['interval_s']} s, {ph['retention_days']} days",
            ),
            (
                "Solar forecast",
                f"{_fmt(so['configured'])} (array {so['pv_kwp']} kWp, tilt "
                f"{so['pv_tilt']}, azimuth {so['pv_azimuth']})",
            ),
            ("NEM region", f["nem_region"]),
            ("Services / dynamic tariff", f"{f['services']} / {_fmt(f['dynamic_tariff'])}"),
            ("Users by role", ", ".join(f"{k} {v}" for k, v in f["users_by_role"].items())),
        ],
    )
    p = info["recent_problems"]
    L += [
        "",
        f"#### Recent problems (last {p['window_hours']} h)",
        f"- {p['warnings']} warnings, {p['errors']} errors",
    ]
    for src, counts in list(p["by_source"].items())[:15]:
        L.append(f"  - {src}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if p["last"]:
        L += ["", f"Last {len(p['last'])} warnings/errors (UTC, scrubbed):", "", "```text"]
        for e in p["last"]:
            gw = f" [{e['gateway']}]" if e.get("gateway") else ""
            msg = e["message"].replace("```", "'''").replace("\n", " ⏎ ")
            L.append(f"{e['time']} {e['level']:<7} {e['source']}{gw}: {msg}")
        L.append("```")
    L += ["", "_Review before posting: it is redacted, but you are responsible for what you post._"]
    return "\n".join(L) + "\n"


__all__ = [
    "INSTALL_DATE_KEY",
    "build_support_info",
    "ensure_install_date",
    "header_note",
    "render_markdown",
]
