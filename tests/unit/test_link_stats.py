"""Link-health measurement (Phase 0b, design §5.4).

The baseline Phase 2 is judged against is only as good as these counters, so
they are tested for the things that would quietly make the baseline wrong:
overlaps not seen, timeouts lumped together, an hour's numbers bleeding into
the next, a failed flush losing an hour, and "not measured" stored as zero.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway import link_stats
from franklinwh_bridge.gateway.link_stats import LinkCollector, classify
from franklinwh_bridge.store import link_stats as store
from franklinwh_bridge.store.db import create_gateway, init_db


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:  # an hour boundary
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "t.db")
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(link_stats, "_registry", {})
    monkeypatch.setattr(link_stats, "_addr_to_gw", {})


def _req(hour: dict, kind: str, path: str) -> dict:
    return hour["requests"][(kind, path)]


# ── Classification ────────────────────────────────────────────


def test_peer_close_and_no_reply_are_counted_apart():
    """C16: pysunspec2 says "Response timeout" when recv() returns 0 bytes,
    i.e. the aGate closed the connection. That is not the same failure as no
    answer at all, and the baseline has to keep them apart."""
    from sunspec2.modbus.modbus import ModbusClientError, ModbusClientTimeout

    assert classify(ModbusClientTimeout("Response timeout"))[0] == "timeout_closed"
    assert classify(ConnectionResetError())[0] == "timeout_closed"
    assert classify(TimeoutError("timed out"))[0] == "timeout_no_reply"
    assert classify(ModbusClientError("Connection error: timed out"))[0] == "timeout_no_reply"
    assert classify(ValueError("bad scale factor"))[0] == "error"


def test_error_classes_mask_numbers_so_they_stay_few():
    a = classify(RuntimeError("Modbus exception 2: addr: 15013 count: 1"))[1]
    b = classify(RuntimeError("Modbus exception 2: addr: 16000 count: 3"))[1]
    assert a == b == "RuntimeError: Modbus exception #: addr: # count: #"


# ── Requests ──────────────────────────────────────────────────


def test_outcomes_latency_and_results_are_recorded():
    c = LinkCollector("gw", clock=Clock())
    with c.track("model_read", "poller") as r:
        r.check({"soc": 50})
    with c.track("model_read", "poller") as r:
        r.check({})  # the library swallowed a failure
    with pytest.raises(TimeoutError), c.track("model_read", "poller"):
        raise TimeoutError("timed out")

    class ErrResp:
        def isError(self):  # noqa: N802 - pymodbus API
            return True

    with c.track("vreg_read", "poller") as r:
        r.check(ErrResp())

    (hour,) = c.drain(final=True)
    mr = _req(hour, "model_read", "poller")
    assert (mr["count"], mr["ok"], mr["empty"], mr["timeout_no_reply"]) == (3, 1, 1, 1)
    assert mr["lat_p50_ms"] is not None and mr["lat_max_ms"] >= mr["lat_p50_ms"]
    assert _req(hour, "vreg_read", "poller")["error"] == 1


def test_percentiles_are_nearest_rank():
    b = link_stats._ReqBucket()
    for ms in range(1, 101):
        b.count += 1
        b.add_latency(float(ms))
    row = b.row()
    assert (row["lat_p50_ms"], row["lat_p95_ms"], row["lat_max_ms"]) == (50.0, 95.0, 100.0)


def test_latency_samples_stay_bounded(monkeypatch):
    monkeypatch.setattr(link_stats, "MAX_SAMPLES", 10)
    b = link_stats._ReqBucket()
    for ms in range(1000):
        b.count += 1
        b.add_latency(float(ms))
    assert len(b.samples) == 10
    assert b.row()["lat_max_ms"] == 999.0  # the max is exact, not sampled


# ── Overlap (U10) ─────────────────────────────────────────────


def test_a_request_starting_while_another_is_in_flight_is_an_overlap():
    c = LinkCollector("gw", clock=Clock())
    inside = threading.Event()
    release = threading.Event()

    def poller():
        with c.track("model_read", "poller"):
            inside.set()
            release.wait(2)

    t = threading.Thread(target=poller)
    t.start()
    assert inside.wait(2)
    with c.track("probe", "health"):
        pass
    release.set()
    t.join()
    with c.track("model_read", "poller"):  # after: no overlap
        pass

    link = c.drain(final=True)[0]["link"]
    assert link["overlaps"] == 1
    assert link["overlap_pairs"] == {"poller>health": 1}
    assert link["max_in_flight"] == 2


def test_sequential_requests_are_not_overlaps():
    c = LinkCollector("gw", clock=Clock())
    for _ in range(3):
        with c.track("model_read", "poller"):
            pass
    link = c.drain(final=True)[0]["link"]
    assert (link["overlaps"], link["max_in_flight"]) == (0, 1)


# ── Hours ─────────────────────────────────────────────────────


def test_hour_rollover_seals_the_previous_hour():
    clock = Clock()
    c = LinkCollector("gw", clock=clock)
    with c.track("model_read", "poller"):
        pass
    clock.t += 3600 + 5
    with c.track("model_read", "poller"):
        pass

    sealed = c.drain()
    assert [h["hour_start"] for h in sealed] == [1_800_000_000.0]
    assert sealed[0]["partial"] is False
    assert _req(sealed[0], "model_read", "poller")["count"] == 1

    (rest,) = c.drain(final=True)
    assert rest["hour_start"] == 1_800_003_600.0 and rest["partial"] is True


def test_newest_good_age_and_failure_runs():
    clock = Clock()
    c = LinkCollector("gw", clock=clock)
    c.note_poll(good=True, from_cache=False)
    clock.t += 10
    c.note_poll(good=False, from_cache=True)
    clock.t += 10
    c.note_poll(good=False, from_cache=True)
    clock.t += 5

    link = c.drain(final=True)[0]["link"]
    assert (link["polls"], link["cache_polls"], link["max_consec_fail"]) == (3, 2, 2)
    assert link["newest_good_age_s"] == 25.0


def test_an_idle_hour_writes_nothing():
    c = LinkCollector("gw", clock=Clock())
    assert c.drain(final=True) == []


# ── Persistence ───────────────────────────────────────────────


async def test_migration_creates_the_table(db):
    async with db.execute("PRAGMA table_info(link_stats)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert {"gateway_id", "hour_start", "kind", "path", "timeout_closed",
            "lat_p95_ms", "overlap_pairs", "max_established", "partial"} <= cols


async def test_flush_writes_request_and_link_rows(db):
    c = link_stats.collector("gw")
    with c.track("model_read", "poller"):
        pass
    c.note_connection("vendor")
    assert await store.flush(db, final=True) == 2

    rows = await store.query(db, "gw", 0)
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["model_read"]["ok"] == 1
    assert by_kind["link"]["conns_opened"] == {"vendor": 1}
    assert by_kind["link"]["partial"] == 1
    assert await store.flush(db) == 0  # nothing left to write


async def test_a_failed_flush_keeps_the_hours(db):
    c = link_stats.collector("gw")
    with c.track("write", "command"):
        pass
    await db.execute("DROP TABLE link_stats")
    assert await store.flush(db, final=True) == 0
    assert len(c.peek()) == 1  # put back, not lost


async def test_purge_drops_rows_past_retention(db):
    await db.execute(
        "INSERT INTO link_stats (gateway_id, hour_start, kind) VALUES ('gw', ?, 'link')",
        (time.time() - 91 * 86400,),
    )
    await db.execute(
        "INSERT INTO link_stats (gateway_id, hour_start, kind) VALUES ('gw', ?, 'link')",
        (time.time(),),
    )
    assert await store.purge(db) == 1


# ── Library hooks ─────────────────────────────────────────────


def test_library_retry_warnings_count_against_the_tracked_gateway(monkeypatch):
    """The library's retry line names no gateway; it belongs to the gateway
    whose request is running on that thread."""
    # An earlier test may have run install(); count with this handler only.
    monkeypatch.setattr(logging.getLogger("franklinwh_modbus"), "handlers", [])
    handler = link_stats.LibraryLogCounter()
    lib = logging.getLogger("franklinwh_modbus.controller")
    lib.addHandler(handler)
    try:
        # Outside any tracked request: no gateway to charge it to.
        lib.warning("Connection issue (Response timeout), attempt 1/2, reconnecting...")
        with link_stats.track("gw", "model_read", "poller"):
            lib.warning("Connection issue (Response timeout), attempt 1/2, reconnecting...")
    finally:
        lib.removeHandler(handler)

    link = link_stats.collector("gw").drain(final=True)[0]["link"]
    assert link["lib_retries"] == 1
    assert link["error_classes"] == {"Response timeout": 1}


def test_library_rescans_are_timed_and_pass_through(monkeypatch):
    from franklinwh_modbus import controller as ctl

    results = iter([True, False])
    monkeypatch.setattr(ctl.FranklinWHController, "reconnect", lambda self: next(results))
    link_stats._wrap_reconnect()
    fake = ctl.FranklinWHController.__new__(ctl.FranklinWHController)
    with link_stats.track("gw", "model_read", "poller"):
        assert fake.reconnect() is True
        assert fake.reconnect() is False

    link = link_stats.collector("gw").drain(final=True)[0]["link"]
    assert (link["rescans"], link["rescans_failed"], link["reconnects_failed"]) == (2, 1, 0)
    assert link["rescan_max_ms"] is not None


def test_pysunspec2_connects_are_counted_and_still_run(monkeypatch):
    from sunspec2.modbus import modbus as ss

    calls = []
    monkeypatch.setattr(ss.ModbusClientTCP, "connect", lambda self, *a, **k: calls.append(1))
    monkeypatch.setattr(link_stats, "_installed", False)
    monkeypatch.setattr(logging.getLogger("franklinwh_modbus"), "handlers", [])
    link_stats.install()

    link_stats.register_address("gw", "192.0.2.10", 502)
    ss.ModbusClientTCP(ipaddr="192.0.2.10", ipport=502).connect()       # by address
    with link_stats.track("other", "model_read", "poller"):
        ss.ModbusClientTCP(ipaddr="192.0.2.99", ipport=502).connect()   # by thread

    assert calls == [1, 1]
    assert link_stats.collector("gw").peek()[0]["link"]["conns_opened"] == {"library": 1}
    assert link_stats.collector("other").peek()[0]["link"]["conns_opened"] == {"library": 1}


# ── Socket census ─────────────────────────────────────────────


def test_established_sockets_are_counted_from_proc(tmp_path):
    ip = link_stats._ipv4_hex("192.0.2.10")
    proc = tmp_path / "tcp"
    proc.write_text(
        "  sl  local_address rem_address   st\n"
        f"   0: 0100007F:9C40 {ip}:01F6 01 x\n"      # ESTABLISHED to :502
        f"   1: 0100007F:9C41 {ip}:01F6 06 x\n"      # TIME_WAIT: not counted
        f"   2: 0100007F:9C42 {ip}:01F7 01 x\n"      # other port
    )
    assert link_stats.count_established("192.0.2.10", 502, (str(proc),)) == 1


def test_no_proc_means_not_measured_not_zero(tmp_path):
    assert link_stats.count_established("192.0.2.10", 502, (str(tmp_path / "nope"),)) is None


# ── REST ──────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from franklinwh_bridge.main import app

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        # A gateway that exists (#296 item 42); no host, so it never starts.
        await create_gateway(app.state.db, "gwx", "GW X", "")
        yield ac, app


async def test_link_stats_endpoint_returns_stored_and_open_hours(client):
    ac, app = client
    c = link_stats.collector("gwx")
    with c.track("model_read", "poller"):
        pass
    await store.flush(app.state.db, final=True)
    with c.track("probe", "health"):
        pass

    resp = await ac.get("/api/gateways/gwx/link-stats?hours=2")
    assert resp.status_code == 200
    body = resp.json()
    assert {r["kind"] for r in body["rows"]} == {"model_read", "link"}
    assert any(r["kind"] == "probe" and r["ok"] == 1 for r in body["open_hour"])


async def test_link_stats_hours_are_bounded(client):
    ac, _ = client
    assert (await ac.get("/api/gateways/gwx/link-stats?hours=0")).status_code == 422
    assert (await ac.get("/api/gateways/gwx/link-stats?hours=99999")).status_code == 422


# ── Call sites ────────────────────────────────────────────────


async def test_a_poll_records_model_and_vendor_reads(monkeypatch):
    """One poll: every library read and each of the three vendor-register
    ranges is a tracked request, and the poll itself is counted."""
    from unittest.mock import MagicMock

    from franklinwh_bridge.modbus.poller import POLL_METHODS, ModbusPoller
    from franklinwh_bridge.modbus.sample import SampleBus

    ctrl = MagicMock()
    ctrl.ip_address, ctrl.port, ctrl.unit_id = "192.0.2.10", 502, 1
    ctrl.connect.return_value = True
    ctrl.get_model.return_value = None
    for name in POLL_METHODS:
        getattr(ctrl, name).return_value = {"x": 1}
    getattr(ctrl, POLL_METHODS[-1]).return_value = {}  # counted as empty, not ok

    resp = MagicMock()
    resp.isError.return_value = False
    resp.registers = [0] * 40
    vclient = MagicMock()
    vclient.read_holding_registers.return_value = resp

    poller = ModbusPoller(controller=ctrl, sample_bus=SampleBus(), gateway_id="gwp")
    monkeypatch.setattr(poller, "_get_vreg_client", lambda: vclient)
    monkeypatch.setattr(link_stats, "count_established", lambda *a, **k: None)
    await poller.poll_once()

    (hour,) = link_stats.collector("gwp").drain(final=True)
    reqs = hour["requests"]
    polled = reqs[("model_read", "poller")]
    assert (polled["count"], polled["empty"]) == (len(POLL_METHODS), 1)
    assert reqs[("vreg_read", "poller")]["count"] == 3
    assert reqs[("connect", "poller")]["ok"] == 1
    assert hour["link"]["polls"] == 1
    assert hour["link"]["max_established"] is None  # not measured here


async def test_command_writes_are_tracked_and_false_is_an_error(db):
    from unittest.mock import MagicMock

    from franklinwh_bridge.publish.command_handler import CommandHandler

    def broken():
        raise OSError("Broken pipe")

    h = CommandHandler(MagicMock(), db, gateway_id="gwc")
    assert h._io("write", lambda: (False, "rejected")) == (False, "rejected")
    assert h._io("write", lambda: (True, "ok")) == (True, "ok")
    with pytest.raises(OSError):
        h._io("write", broken)

    w = link_stats.collector("gwc").drain(final=True)[0]["requests"][("write", "command")]
    assert (w["count"], w["ok"], w["error"], w["timeout_closed"]) == (3, 1, 1, 1)
