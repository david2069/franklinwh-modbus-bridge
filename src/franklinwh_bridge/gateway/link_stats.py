"""Link-health measurement: every request this bridge makes to an aGate.

Phase 0b of docs/dispatch-reliability-design.md §5.4. Read-only
instrumentation: it times and classifies requests and counts connection
events. Nothing here retries, reorders or blocks a request, and a failure
inside the collector is swallowed, never raised into the caller.

On 2026-10-12 the connection picture had to be rebuilt by hand from
untimestamped library lines and 2 s socket sampling, and parts of it were
wrong. This records the same facts continuously, per gateway, rolled up per
hour, so there is a baseline before Phase 2 changes anything (acceptance A8).

**Collection.** Blocking Modbus calls run in worker threads, so the collector
is thread-safe (one lock per gateway; the work under it is a few counter
updates). Call sites wrap a request in :func:`track`::

    with link_stats.track(gw_id, "model_read", "poller") as req:
        result = controller.read_battery_status()
        req.check(result)          # optional: classify a non-raising result

An exception leaving the block is classified and re-raised unchanged.

**What a row means** (table ``link_stats``, written by
:mod:`franklinwh_bridge.store.link_stats`):

- one row per ``(gateway, hour, kind, path)`` for requests. ``kind`` is
  ``model_read`` / ``vreg_read`` / ``write`` / ``connect`` / ``probe`` /
  ``diag``; ``path`` is the code path that sent it (``poller``, ``startup``,
  ``command``, ``health``, ``diagnostics``, ``explorer``, ...). Outcomes:
  ``ok``, ``empty`` (the call returned nothing, which the library does when
  it swallows a failure), ``timeout_no_reply`` (no answer before the socket
  timeout), ``timeout_closed`` (the peer closed the connection: pysunspec2
  raises "Response timeout" when ``recv`` returns 0 bytes, design C16), and
  ``error``. Latency p50/p95 come from a bounded sample of the hour.
- one row per ``(gateway, hour)`` with ``kind = 'link'`` for everything that
  is not a single request: connections opened per path (``library`` = a
  pysunspec2 TCP connect, ``vendor`` = the pymodbus vendor-register client,
  ``probe`` = a raw TCP reachability probe), bridge reconnects, library
  rescans (its ``reconnect()``, which re-runs the SunSpec scan) and their
  longest duration, library retries, the longest run of failed polls, the age
  of the newest good poll when the hour closed, polls published from cached
  values (C8), overlapping requests (U10) with the path pair, the most
  requests in flight at once, and the most ESTABLISHED sockets to the aGate
  seen in any poll (Linux ``/proc/net/tcp`` only; NULL elsewhere).

A row flushed at shutdown, before its hour ended, has ``partial = 1``; a
restart inside an hour therefore leaves two rows for it, which readers sum.
"""

from __future__ import annotations

import contextlib
import logging
import random
import re
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Latency samples kept per (kind, path) per hour; reservoir-sampled beyond.
MAX_SAMPLES = 2000
#: Distinct error classes kept per bucket; the rest are counted as "other".
MAX_ERROR_CLASSES = 30

KINDS = ("model_read", "vreg_read", "write", "connect", "probe", "diag")

# ── Classification ───────────────────────────────────────────

_CLOSED_MARKERS = (
    "response timeout",          # pysunspec2: recv() returned 0 bytes (C16)
    "connection closed",
    "unexpectedly closed",
    "connection reset",
    "broken pipe",
)
_NO_REPLY_MARKERS = ("timed out", "timeout", "no response")


def classify(exc_or_text: BaseException | str) -> tuple[str, str]:
    """Return ``(outcome, error_class)`` for a failure.

    ``outcome`` is ``timeout_closed``, ``timeout_no_reply`` or ``error``. The
    class is the type plus the first words of the message with numbers
    masked, so "addr: 15013" and "addr: 15014" count as one class.
    """
    if isinstance(exc_or_text, BaseException):
        name = type(exc_or_text).__name__
        text = str(exc_or_text) or name
    else:
        name, text = "", str(exc_or_text)
    low = text.lower()
    if isinstance(exc_or_text, (ConnectionResetError, BrokenPipeError)) or any(
        m in low for m in _CLOSED_MARKERS
    ):
        outcome = "timeout_closed"
    elif isinstance(exc_or_text, TimeoutError) or any(m in low for m in _NO_REPLY_MARKERS):
        outcome = "timeout_no_reply"
    else:
        outcome = "error"
    msg = re.sub(r"\d+", "#", text)[:60]
    return outcome, f"{name}: {msg}" if name else msg


def _bump_class(classes: Counter, key: str) -> None:
    if key in classes or len(classes) < MAX_ERROR_CLASSES:
        classes[key] += 1
    else:
        classes["other"] += 1


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_vals:
        return None
    rank = max(1, int(round(pct / 100.0 * len(sorted_vals) + 0.4999)))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


# ── Buckets ──────────────────────────────────────────────────


@dataclass
class _ReqBucket:
    count: int = 0
    ok: int = 0
    empty: int = 0
    timeout_no_reply: int = 0
    timeout_closed: int = 0
    error: int = 0
    error_classes: Counter = field(default_factory=Counter)
    samples: list[float] = field(default_factory=list)
    lat_max_ms: float | None = None

    def add_latency(self, ms: float) -> None:
        # count already includes this request, so it is the reservoir's n.
        if len(self.samples) < MAX_SAMPLES:
            self.samples.append(ms)
        else:
            j = random.randrange(self.count)
            if j < MAX_SAMPLES:
                self.samples[j] = ms
        if self.lat_max_ms is None or ms > self.lat_max_ms:
            self.lat_max_ms = ms

    def row(self) -> dict[str, Any]:
        s = sorted(self.samples)
        return {
            "count": self.count, "ok": self.ok, "empty": self.empty,
            "timeout_no_reply": self.timeout_no_reply,
            "timeout_closed": self.timeout_closed, "error": self.error,
            "error_classes": dict(self.error_classes),
            "lat_p50_ms": _percentile(s, 50), "lat_p95_ms": _percentile(s, 95),
            "lat_max_ms": self.lat_max_ms,
        }


@dataclass
class _LinkBucket:
    conns_opened: Counter = field(default_factory=Counter)
    reconnects: int = 0
    reconnects_failed: int = 0
    rescans: int = 0
    rescans_failed: int = 0
    rescan_max_ms: float | None = None
    lib_retries: int = 0
    lib_classes: Counter = field(default_factory=Counter)
    max_consec_fail: int = 0
    polls: int = 0
    cache_polls: int = 0
    overlaps: int = 0
    overlap_pairs: Counter = field(default_factory=Counter)
    max_in_flight: int = 0
    max_established: int | None = None

    def row(self, newest_good_age_s: float | None) -> dict[str, Any]:
        return {
            "conns_opened": dict(self.conns_opened),
            "reconnects": self.reconnects, "reconnects_failed": self.reconnects_failed,
            "rescans": self.rescans, "rescans_failed": self.rescans_failed,
            "rescan_max_ms": self.rescan_max_ms,
            "lib_retries": self.lib_retries, "error_classes": dict(self.lib_classes),
            "max_consec_fail": self.max_consec_fail,
            "newest_good_age_s": newest_good_age_s,
            "polls": self.polls, "cache_polls": self.cache_polls,
            "overlaps": self.overlaps, "overlap_pairs": dict(self.overlap_pairs),
            "max_in_flight": self.max_in_flight, "max_established": self.max_established,
        }


def _hour_of(ts: float) -> float:
    return float(int(ts // 3600) * 3600)


class _Request:
    """Handle yielded by :func:`track`; lets the caller classify a result."""

    __slots__ = ("_failure", "_empty")

    def __init__(self) -> None:
        self._failure: BaseException | str | None = None
        self._empty = False

    def fail(self, reason: BaseException | str) -> None:
        """Mark the request failed without raising (e.g. pymodbus isError())."""
        self._failure = reason

    def check(self, result: Any) -> Any:
        """Classify a returned value: ``False`` or a pymodbus error response
        fails the request, an empty dict/None counts as ``empty``. Returns
        *result*."""
        try:
            if result is False:
                self._failure = "returned False"
            elif result is None or (isinstance(result, dict) and not result):
                self._empty = True
            elif callable(getattr(result, "isError", None)) and result.isError():
                self._failure = str(result)
        except Exception:  # pragma: no cover - never break the caller
            pass
        return result


class LinkCollector:
    """In-memory hourly buckets for one gateway. Thread-safe."""

    def __init__(self, gateway_id: str, clock: Callable[[], float] = time.time) -> None:
        self.gateway_id = gateway_id
        self._clock = clock
        self._lock = threading.Lock()
        self._hour = _hour_of(clock())
        self._req: dict[tuple[str, str], _ReqBucket] = {}
        self._link = _LinkBucket()
        self._sealed: list[dict[str, Any]] = []
        self._inflight: dict[int, str] = {}
        self._next_token = 0
        self._consec_fail = 0
        self._last_good_ts: float | None = None

    # -- hour handling (call with the lock held) --

    def _roll(self, now: float) -> None:
        hour = _hour_of(now)
        if hour != self._hour:
            self._seal(end=self._hour + 3600, partial=False)
            self._hour = hour

    def _snapshot(self, end: float, partial: bool) -> dict[str, Any] | None:
        if not self._req and self._link == _LinkBucket():
            return None
        age = None if self._last_good_ts is None else max(0.0, end - self._last_good_ts)
        return {
            "gateway_id": self.gateway_id, "hour_start": self._hour, "partial": partial,
            "requests": {k: b.row() for k, b in self._req.items()},
            "link": self._link.row(age),
        }

    def _seal(self, end: float, partial: bool) -> None:
        snap = self._snapshot(end, partial)
        if snap is not None:
            self._sealed.append(snap)
        self._req = {}
        self._link = _LinkBucket()
        # A run of failures spans hours; carry it so the next hour's max
        # starts from where it is, not from zero.
        self._link.max_consec_fail = self._consec_fail

    # -- recording --

    @contextlib.contextmanager
    def track(self, kind: str, path: str) -> Iterator[_Request]:
        req = _Request()
        with self._lock:
            self._roll(self._clock())
            if self._inflight:
                other = next(iter(self._inflight.values()))
                self._link.overlaps += 1
                _bump_class(self._link.overlap_pairs, f"{other}>{path}")
            token = self._next_token
            self._next_token += 1
            self._inflight[token] = path
            self._link.max_in_flight = max(self._link.max_in_flight, len(self._inflight))
        prev_gw = getattr(_current, "gateway_id", None)
        _current.gateway_id = self.gateway_id
        t0 = time.monotonic()
        failure: BaseException | str | None = None
        try:
            yield req
        except BaseException as exc:
            failure = exc
            raise
        finally:
            ms = (time.monotonic() - t0) * 1000.0
            _current.gateway_id = prev_gw
            failure = failure if failure is not None else req._failure
            with self._lock:
                self._inflight.pop(token, None)
                self._roll(self._clock())
                b = self._req.setdefault((kind, path), _ReqBucket())
                b.count += 1
                b.add_latency(ms)
                if failure is not None:
                    outcome, cls = classify(failure)
                    setattr(b, outcome, getattr(b, outcome) + 1)
                    _bump_class(b.error_classes, cls)
                elif req._empty:
                    b.empty += 1
                else:
                    b.ok += 1

    def note_connection(self, path: str) -> None:
        with self._lock:
            self._roll(self._clock())
            self._link.conns_opened[path] += 1

    def note_reconnect(self, ok: bool) -> None:
        with self._lock:
            self._roll(self._clock())
            self._link.reconnects += 1
            if not ok:
                self._link.reconnects_failed += 1

    def note_rescan(self, duration_ms: float | None, ok: bool) -> None:
        with self._lock:
            self._roll(self._clock())
            self._link.rescans += 1
            if not ok:
                self._link.rescans_failed += 1
            if duration_ms is not None and (
                self._link.rescan_max_ms is None or duration_ms > self._link.rescan_max_ms
            ):
                self._link.rescan_max_ms = duration_ms

    def note_lib_retry(self, text: str) -> None:
        with self._lock:
            self._roll(self._clock())
            self._link.lib_retries += 1
            _bump_class(self._link.lib_classes, classify(text)[1])

    def note_poll(self, *, good: bool, from_cache: bool) -> None:
        """One published poll. *good* = at least one read succeeded."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            self._link.polls += 1
            if from_cache:
                self._link.cache_polls += 1
            if good:
                self._consec_fail = 0
                self._last_good_ts = now
            else:
                self._consec_fail += 1
                self._link.max_consec_fail = max(self._link.max_consec_fail, self._consec_fail)

    def note_established(self, n: int | None) -> None:
        if n is None:
            return
        with self._lock:
            self._roll(self._clock())
            cur = self._link.max_established
            self._link.max_established = n if cur is None else max(cur, n)

    # -- draining --

    def drain(self, final: bool = False) -> list[dict[str, Any]]:
        """Return sealed hours (and, if *final*, the current partial hour)."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            if final:
                self._seal(end=now, partial=True)
            out, self._sealed = self._sealed, []
            return out

    def peek(self) -> list[dict[str, Any]]:
        """Unwritten hours plus the open one, without draining anything."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            snap = self._snapshot(end=now, partial=True)
            return [*self._sealed, *([snap] if snap else [])]

    def restore(self, rows: list[dict[str, Any]]) -> None:
        """Put back rows a failed flush could not write."""
        with self._lock:
            self._sealed[:0] = rows


# ── Registry and module-level helpers ────────────────────────

_registry: dict[str, LinkCollector] = {}
_registry_lock = threading.Lock()
_addr_to_gw: dict[tuple[str, int], str] = {}
_current = threading.local()


def collector(gateway_id: str) -> LinkCollector:
    with _registry_lock:
        c = _registry.get(gateway_id)
        if c is None:
            c = _registry[gateway_id] = LinkCollector(gateway_id)
        return c


def find(gateway_id: str) -> LinkCollector | None:
    """The gateway's collector if anything has been recorded for it."""
    with _registry_lock:
        return _registry.get(gateway_id)


def collectors() -> list[LinkCollector]:
    with _registry_lock:
        return list(_registry.values())


def register_address(gateway_id: str, host: str, port: int) -> None:
    """Map host:port to a gateway so library connects can be attributed even
    when they happen outside a tracked request."""
    with _registry_lock:
        _addr_to_gw[(str(host), int(port))] = gateway_id
    collector(gateway_id)


def track(gateway_id: str, kind: str, path: str) -> contextlib.AbstractContextManager[_Request]:
    return collector(gateway_id).track(kind, path)


def _safe(fn: Callable[..., None]) -> Callable[..., None]:
    def inner(gateway_id: str | None, *a: Any, **kw: Any) -> None:
        if not gateway_id:
            return
        try:
            fn(collector(gateway_id), *a, **kw)
        except Exception as exc:  # pragma: no cover - measurement never raises
            logger.debug("link_stats: %s", exc)
    return inner


note_connection = _safe(LinkCollector.note_connection)
note_reconnect = _safe(LinkCollector.note_reconnect)
note_poll = _safe(LinkCollector.note_poll)
note_established = _safe(LinkCollector.note_established)


def _gateway_for_thread(addr: tuple[str, int] | None = None) -> str | None:
    gw = getattr(_current, "gateway_id", None)
    if gw:
        return gw
    if addr is not None:
        with _registry_lock:
            return _addr_to_gw.get((str(addr[0]), int(addr[1])))
    return None


# ── Library hooks ────────────────────────────────────────────


class LibraryLogCounter(logging.Handler):
    """Counts franklinwh_modbus's internal retries.

    ``controller._with_retry`` retries a failed call after a reconnect and
    only says so in a WARNING ("Connection issue (<error>), attempt ...").
    The line names no gateway, so it is attributed to the gateway whose
    tracked request is running on the same thread. (Rescans are counted by
    wrapping ``reconnect()`` instead: its own log lines are INFO, which the
    bridge never enables for the library, so they never reach a handler.)
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if not msg.startswith("Connection issue ("):
                return
            gw = _gateway_for_thread()
            if gw:
                collector(gw).note_lib_retry(
                    msg[len("Connection issue ("):].rsplit("), attempt", 1)[0]
                )
        except Exception:  # pragma: no cover - a log handler must never raise
            pass


_installed = False


def install() -> None:
    """Attach the library hooks. Idempotent. Each only counts; the wrapped
    call runs unchanged and its result or exception passes straight through.

    - a log handler for the library's retry warnings;
    - ``FranklinWHController.reconnect`` (a rescan: it rebuilds the client
      and re-runs the SunSpec scan), timed;
    - ``ModbusClientTCP.connect`` in pysunspec2 (the library opens a TCP
      connection per call, design C1).
    """
    global _installed
    if _installed:
        return
    _installed = True
    logging.getLogger("franklinwh_modbus").addHandler(LibraryLogCounter())
    _wrap_reconnect()
    try:
        from sunspec2.modbus import modbus as _ss
    except ImportError:  # pragma: no cover - library always present in prod
        return
    orig = _ss.ModbusClientTCP.connect
    if getattr(orig, "_link_stats_wrapped", False):
        return

    def connect(self: Any, *a: Any, **kw: Any) -> Any:
        try:
            gw = _gateway_for_thread((self.ipaddr, self.ipport))
            if gw:
                collector(gw).note_connection("library")
        except Exception:  # pragma: no cover
            pass
        return orig(self, *a, **kw)

    connect._link_stats_wrapped = True  # type: ignore[attr-defined]
    _ss.ModbusClientTCP.connect = connect


def _wrap_reconnect() -> None:
    try:
        from franklinwh_modbus import controller as _ctl
    except ImportError:  # pragma: no cover
        return
    cls = _ctl.FranklinWHController
    orig = cls.reconnect
    if getattr(orig, "_link_stats_wrapped", False):
        return

    def reconnect(self: Any, *a: Any, **kw: Any) -> Any:
        gw = _gateway_for_thread((getattr(self, "ip_address", ""), getattr(self, "port", 0)))
        t0 = time.monotonic()
        ok = False
        try:
            ok = bool(orig(self, *a, **kw))
            return ok
        finally:
            if gw:
                with contextlib.suppress(Exception):
                    collector(gw).note_rescan((time.monotonic() - t0) * 1000.0, ok=ok)

    reconnect._link_stats_wrapped = True  # type: ignore[attr-defined]
    cls.reconnect = reconnect


# ── Socket census ────────────────────────────────────────────

_resolved: dict[str, str | None] = {}


def _ipv4_hex(ip: str) -> str:
    """/proc/net/tcp stores IPv4 as little-endian hex."""
    return socket.inet_aton(ip)[::-1].hex().upper()


def count_established(
    host: str, port: int, proc_paths: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6"),
) -> int | None:
    """ESTABLISHED sockets to host:port in this network namespace.

    Returns None where /proc/net/tcp does not exist (macOS) or the host does
    not resolve, so "not measured" is never recorded as zero.
    """
    if host not in _resolved:
        try:
            _resolved[host] = socket.gethostbyname(host)
        except OSError:
            _resolved[host] = None
    ip = _resolved[host]
    if not ip:
        return None
    v4 = f"{_ipv4_hex(ip)}:{port:04X}"
    v6 = f"0000000000000000FFFF0000{_ipv4_hex(ip)}:{port:04X}"
    found = False
    n = 0
    for p in proc_paths:
        try:
            with open(p) as fh:
                lines = fh.readlines()[1:]
        except OSError:
            continue
        found = True
        for line in lines:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "01" and parts[2].upper() in (v4, v6):
                n += 1
    return n if found else None
