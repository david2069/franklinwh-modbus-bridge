"""The bridge's log reaches the console (HA add-on Log, docker logs), timestamped,
and routine successful GETs don't drown it unless at DEBUG."""

from __future__ import annotations

import logging

from franklinwh_bridge.main import _QuietAccessLog, configure_console_logging


def _access(method: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", method, "/api/status", "1.1", status), None,
    )


def test_access_log_keeps_writes_and_errors_drops_routine_gets():
    logging.getLogger("franklinwh_bridge").setLevel(logging.INFO)
    f = _QuietAccessLog()
    assert not f.filter(_access("GET", 200))
    assert f.filter(_access("GET", 404))
    assert f.filter(_access("GET", 500))
    assert f.filter(_access("POST", 200))
    assert f.filter(_access("PATCH", 200))


def test_debug_keeps_every_request():
    bridge = logging.getLogger("franklinwh_bridge")
    old = bridge.level
    try:
        bridge.setLevel(logging.DEBUG)
        assert _QuietAccessLog().filter(_access("GET", 200))
    finally:
        bridge.setLevel(old)


def test_console_handler_is_added_once_and_timestamped():
    bridge = logging.getLogger("franklinwh_bridge")
    configure_console_logging(logging.INFO)
    configure_console_logging(logging.INFO)  # the app boots many times per process
    consoles = [h for h in bridge.handlers if getattr(h, "_bridge_console", False)]
    assert len(consoles) == 1
    line = consoles[0].format(logging.LogRecord(
        "franklinwh_bridge.x", logging.WARNING, __file__, 1, "hello", None, None))
    assert line.endswith("WARNING franklinwh_bridge.x: hello")
    assert line[:4].isdigit() and line[4] == "-"   # 2026-10-07 22:54:02 …
