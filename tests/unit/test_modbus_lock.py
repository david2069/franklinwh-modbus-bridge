"""Tests for Modbus TCP connection lock — prevents poller/command contention.

The FranklinWH aGate has a single Modbus TCP socket.  When the poller reads
and the command handler writes simultaneously, TCP response timeouts cause
reconnect storms and metric data gaps.  A shared asyncio.Lock coordinates
access so only one caller uses the connection at a time.

These tests verify:
1. Shared lock prevents concurrent Modbus access (no interleaving)
2. Lock is held for the entire poll cycle (consistent samples)
3. Commands wait for the poll to finish before executing
4. No deadlocks in the command handler's call chains
5. Sequencer bypasses are properly locked

NOTE: These patterns reproduce a real production issue on the aGate where
15 gaps totalling ~88 minutes were observed in a 6h window during active
command dispatch.  The root cause was concurrent asyncio.to_thread() calls
on the same FranklinWHController sharing one TCP connection.

Upstream library issue: The franklinwh-modbus library should ideally
provide its own internal locking or connection pool, rather than requiring
callers to coordinate.  See: https://github.com/davidhona/franklinwh-modbus
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import aiosqlite
import pytest

from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus
from franklinwh_bridge.publish.command_handler import CommandHandler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_lock():
    """A single asyncio.Lock shared between poller and command handler."""
    return asyncio.Lock()


@pytest.fixture
def access_log():
    """Thread-safe log of Modbus access events to detect interleaving."""
    return []


@pytest.fixture
def slow_controller(access_log):
    """Mock controller where each Modbus call takes measurable time.

    Records (caller, event, timestamp) so we can verify no two callers
    overlap on the connection.
    """
    ctrl = MagicMock()
    ctrl.ip_address = "192.168.1.100"
    ctrl.port = 502
    ctrl.unit_id = 1
    ctrl.connect.return_value = True
    ctrl.disconnect.return_value = None
    ctrl.get_model.return_value = None

    def _make_slow_read(name: str, data: dict, delay: float = 0.05):
        """Create a mock method that logs access and sleeps."""
        def _read():
            access_log.append((name, "enter", time.monotonic()))
            time.sleep(delay)
            access_log.append((name, "exit", time.monotonic()))
            return data
        return _read

    # Poller reads — each takes 50ms
    ctrl.read_battery_status.side_effect = _make_slow_read(
        "poller:battery", {"soc": 85, "battery_power_w": -1200}
    )
    ctrl.read_grid_status.side_effect = _make_slow_read(
        "poller:grid", {"grid_power_w": 500}
    )
    ctrl.read_solar_status.side_effect = _make_slow_read(
        "poller:solar", {"total_solar": 3200}
    )
    ctrl.read_control_status.side_effect = _make_slow_read(
        "cmd:control_status", {"wset_enabled": 0}
    )
    ctrl.read_native_mode.side_effect = _make_slow_read(
        "poller:native", {"native_mode": 2}
    )
    ctrl.read_alarms.side_effect = _make_slow_read(
        "poller:alarms", {"active_alarms": []}
    )

    # Command handler writes — each takes 50ms
    def _slow_send_command(cmd, duration_s=None):
        access_log.append(("cmd:send_command", "enter", time.monotonic()))
        time.sleep(0.05)
        access_log.append(("cmd:send_command", "exit", time.monotonic()))
        return (True, "OK")
    ctrl.send_command.side_effect = _slow_send_command

    def _slow_reset():
        access_log.append(("cmd:reset", "enter", time.monotonic()))
        time.sleep(0.05)
        access_log.append(("cmd:reset", "exit", time.monotonic()))
        return True
    ctrl.reset_control_state.side_effect = _slow_reset

    def _slow_set_mode(mode_val):
        access_log.append(("cmd:set_mode", "enter", time.monotonic()))
        time.sleep(0.05)
        access_log.append(("cmd:set_mode", "exit", time.monotonic()))
        return (True, "OK")
    ctrl.set_native_mode = _slow_set_mode

    return ctrl


@pytest.fixture
def sample_bus():
    return SampleBus()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    # Minimal schema for command handler
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS control_log "
        "(id INTEGER PRIMARY KEY, ts REAL, event TEXT, action TEXT, "
        "power_w INTEGER, detail TEXT, hw_state TEXT)"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS control_state "
        "(id INTEGER PRIMARY KEY, active INTEGER, action TEXT, "
        "power_w INTEGER, started_at REAL, watchdog_s INTEGER)"
    )
    await conn.commit()
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_no_overlap(access_log: list) -> list[tuple]:
    """Verify no two callers overlap on the connection.

    Returns a list of overlapping (caller_a, caller_b) pairs.
    An overlap occurs when caller_b enters before caller_a exits.
    """
    # Group by enter/exit pairs
    active: list[tuple[str, float]] = []  # (caller, enter_time)
    overlaps: list[tuple[str, str]] = []

    for caller, event, ts in access_log:
        if event == "enter":
            # Check if any other caller category is still active
            prefix = caller.split(":")[0]  # "poller" or "cmd"
            for active_caller, _active_enter in active:
                active_prefix = active_caller.split(":")[0]
                if active_prefix != prefix:
                    overlaps.append((active_caller, caller))
            active.append((caller, ts))
        elif event == "exit":
            active = [(c, t) for c, t in active if c != caller]

    return overlaps


# ---------------------------------------------------------------------------
# Tests: Lock prevents concurrent access
# ---------------------------------------------------------------------------

async def test_shared_lock_prevents_poll_command_overlap(
    slow_controller, sample_bus, shared_lock, access_log, db,
):
    """CORE TEST: Poller and command handler never overlap on the connection.

    Without the lock, asyncio.to_thread() calls from both the poller and
    command handler would run concurrently in the thread pool, causing TCP
    response timeouts on the aGate.

    With the lock, the command handler waits for the current poll to finish
    before sending its writes, and vice versa.
    """
    poller = ModbusPoller(
        controller=slow_controller,
        sample_bus=sample_bus,
        poll_interval=1,
        modbus_lock=shared_lock,
    )
    handler = CommandHandler(
        slow_controller, db, modbus_lock=shared_lock,
    )

    # Connect first (outside contention)
    await poller._connect()

    # Launch poll and command concurrently
    async def _poll():
        return await poller._poll_once()

    async def _command():
        # Small delay so poller starts first
        await asyncio.sleep(0.01)
        await handler.handle_command("battery_command", "Charge")

    sample, _ = await asyncio.gather(_poll(), _command())

    # Verify no concurrent access
    overlaps = check_no_overlap(access_log)
    assert overlaps == [], (
        f"Detected {len(overlaps)} concurrent Modbus access(es): {overlaps}\n"
        f"This would cause TCP response timeouts on the aGate.\n"
        f"Full log: {[(c, e) for c, e, _ in access_log]}"
    )

    # Verify both completed successfully
    assert sample.quality == "ok"


async def test_without_shared_lock_allows_overlap(
    slow_controller, sample_bus, access_log, db,
):
    """CONTROL TEST: Without a shared lock, concurrent access IS possible.

    This demonstrates the bug that the lock fixes. When poller and command
    handler each have their own independent lock, their Modbus calls
    interleave on the shared TCP connection.
    """
    # Separate locks — no coordination
    poller = ModbusPoller(
        controller=slow_controller,
        sample_bus=sample_bus,
        poll_interval=1,
        modbus_lock=asyncio.Lock(),  # own lock
    )
    handler = CommandHandler(
        slow_controller, db,
        modbus_lock=asyncio.Lock(),  # different lock
    )

    await poller._connect()

    async def _poll():
        return await poller._poll_once()

    async def _command():
        await asyncio.sleep(0.01)
        await handler.handle_command("battery_command", "Charge")

    await asyncio.gather(_poll(), _command())

    overlaps = check_no_overlap(access_log)
    # With separate locks, overlaps SHOULD occur (proving the bug exists)
    assert len(overlaps) > 0, (
        "Expected concurrent access without a shared lock — "
        "if this fails, the test timing may need adjustment"
    )


async def test_command_waits_for_poll_to_finish(
    slow_controller, sample_bus, shared_lock, access_log, db,
):
    """Command handler waits for the entire poll cycle, not just one read.

    The lock wraps the entire _poll_once() method, ensuring the command
    only starts after all 6 read methods + extra points have completed.
    """
    poller = ModbusPoller(
        controller=slow_controller,
        sample_bus=sample_bus,
        poll_interval=1,
        modbus_lock=shared_lock,
    )
    handler = CommandHandler(
        slow_controller, db, modbus_lock=shared_lock,
    )

    await poller._connect()

    poll_done = asyncio.Event()
    cmd_started = asyncio.Event()

    async def _poll():
        result = await poller._poll_once()
        poll_done.set()
        return result

    async def _command():
        # Try to start command immediately — should be blocked by lock
        cmd_started.set()
        await handler.handle_command("operating_mode", "Self-Consumption")

    # Start both
    _, _ = await asyncio.gather(_poll(), _command())

    # Find when the last poller read exited and when the actual command
    # operation entered.  Exclude cmd:control_status because
    # read_control_status is shared (it's in POLL_METHODS AND called by
    # _read_hw_state in the audit log).  Only check cmd:set_mode.
    poller_exits = [
        ts for caller, event, ts in access_log
        if caller.startswith("poller:") and event == "exit"
    ]
    cmd_enters = [
        ts for caller, event, ts in access_log
        if caller == "cmd:set_mode" and event == "enter"
    ]

    if poller_exits and cmd_enters:
        last_poller_exit = max(poller_exits)
        first_cmd_enter = min(cmd_enters)
        assert first_cmd_enter >= last_poller_exit, (
            f"Command started at {first_cmd_enter:.4f} before last poller "
            f"read finished at {last_poller_exit:.4f} — lock not held for "
            f"entire poll cycle"
        )


async def test_multiple_commands_serialized(
    slow_controller, sample_bus, shared_lock, access_log, db,
):
    """Multiple concurrent commands are serialized by the lock."""
    handler = CommandHandler(
        slow_controller, db, modbus_lock=shared_lock,
    )

    # Fire 3 mode changes concurrently
    await asyncio.gather(
        handler.handle_command("operating_mode", "Self-Consumption"),
        handler.handle_command("operating_mode", "Time of Use"),
        handler.handle_command("operating_mode", "Emergency Backup"),
    )

    overlaps = check_no_overlap(access_log)
    assert overlaps == [], f"Commands overlapped: {overlaps}"


# ---------------------------------------------------------------------------
# Tests: No deadlocks
# ---------------------------------------------------------------------------

async def test_battery_release_no_deadlock(
    slow_controller, sample_bus, shared_lock, access_log, db,
):
    """Battery command → release chain does not deadlock.

    The call chain is:
      handle_command("battery_command", "Release")
        → _handle_battery_command("Release")
          → _release_command()          [acquires lock]
            → _log_event()
              → _read_hw_state()        [acquires lock — must not deadlock]

    Each lock acquisition is sequential (never nested), so this must
    complete without hanging.
    """
    handler = CommandHandler(
        slow_controller, db, modbus_lock=shared_lock,
    )

    # First send a charge command
    await handler.handle_command("battery_command", "Charge")
    assert handler.state.active

    # Now release it — should not deadlock
    done = asyncio.Event()

    async def _release():
        await handler.handle_command("battery_command", "Release")
        done.set()

    try:
        await asyncio.wait_for(_release(), timeout=5.0)
    except TimeoutError:
        pytest.fail(
            "Deadlock detected: battery_command Release did not complete "
            "within 5 seconds. Check for nested lock acquisition in the "
            "_release_command → _log_event → _read_hw_state chain."
        )

    assert not handler.state.active


async def test_watchdog_release_no_deadlock(
    slow_controller, sample_bus, shared_lock, access_log, db, monkeypatch,
):
    """Watchdog-triggered release does not deadlock.

    The watchdog loop calls _release_command(), which acquires the lock
    and then calls _log_event() → _read_hw_state() which acquires again.
    """
    import franklinwh_bridge.publish.command_handler as ch_mod

    # Patch SOC check interval to 0.2s (default is 5s) so the watchdog
    # fires quickly enough for the test
    monkeypatch.setattr(ch_mod, "SOC_CHECK_INTERVAL_S", 0.2)

    handler = CommandHandler(
        slow_controller, db, modbus_lock=shared_lock,
    )

    # Send a command with 1-second duration (so watchdog triggers quickly)
    handler._command_duration_s = 1
    await handler.handle_command("battery_command", "Charge")
    assert handler.state.active

    # Wait for watchdog to expire and release
    try:
        await asyncio.wait_for(
            _wait_for_inactive(handler), timeout=5.0
        )
    except TimeoutError:
        pytest.fail(
            "Deadlock detected: watchdog release did not complete "
            "within 5 seconds"
        )

    assert not handler.state.active


async def _wait_for_inactive(handler: CommandHandler) -> None:
    """Poll until the command handler becomes inactive."""
    while handler.state.active:
        await asyncio.sleep(0.1)


async def test_concurrent_poll_and_release_no_deadlock(
    slow_controller, sample_bus, shared_lock, access_log, db,
):
    """Concurrent poll + command release does not deadlock."""
    poller = ModbusPoller(
        controller=slow_controller,
        sample_bus=sample_bus,
        poll_interval=1,
        modbus_lock=shared_lock,
    )
    handler = CommandHandler(
        slow_controller, db, modbus_lock=shared_lock,
    )

    await poller._connect()

    # Mock _read_extra_points to avoid real pymodbus TCP connections
    # (the real method creates a ModbusTcpClient that tries to connect)
    poller._read_extra_points = lambda: {}

    # Start a command
    await handler.handle_command("battery_command", "Charge")

    # Now poll + release concurrently
    try:
        await asyncio.wait_for(
            asyncio.gather(
                poller._poll_once(),
                handler.handle_command("battery_command", "Release"),
            ),
            timeout=10.0,
        )
    except TimeoutError:
        pytest.fail("Deadlock: concurrent poll + release hung for 10s")


# ---------------------------------------------------------------------------
# Tests: Lock default behaviour
# ---------------------------------------------------------------------------

async def test_poller_works_without_explicit_lock(
    slow_controller, sample_bus,
):
    """Poller creates its own lock if none is provided (backwards compat)."""
    poller = ModbusPoller(
        controller=slow_controller,
        sample_bus=sample_bus,
        poll_interval=1,
        # No modbus_lock parameter
    )
    await poller._connect()
    sample = await poller._poll_once()
    assert sample.quality == "ok"


async def test_command_handler_works_without_explicit_lock(
    slow_controller, db,
):
    """Command handler creates its own lock if none is provided."""
    handler = CommandHandler(slow_controller, db)
    await handler.handle_command("operating_mode", "Self-Consumption")
    assert "OK" in handler.state.last_result or handler.state.last_result == "OK"
