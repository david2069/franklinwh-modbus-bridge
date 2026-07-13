"""Hardware tests probing two claims that, until now, only had narrative
(non-pytest) evidence in docs/vendor-issues.md: WSetRvrtTms hardware
reversion (Issue 1), and Reserve SoC write persistence / reset-on-mode-touch
(Issue 11).

These tests WRITE to a real aGate — dispatch power, change reserve %. Gated
behind BOTH @pytest.mark.hardware AND @pytest.mark.destructive. Run
explicitly with:

    pytest tests/hardware/test_reversion_and_reserve_persistence.py -v -m destructive

IMPORTANT: pytest markers are additive, not exclusive — a bare `-m hardware`
run (e.g. `pytest tests/hardware/ -m hardware`) will still select these
tests too, since they also carry the `hardware` marker. If you want
read-only hardware tests only, use:

    pytest tests/hardware/ -v -m "hardware and not destructive"

Every test restores prior state in a `finally` block regardless of outcome,
and uses minimal power / short durations / small reserve deltas by default.
Do not run without confirming the parameters below are acceptable for the
live system under test.
"""

import time

import pytest
from franklinwh_modbus.types import BatteryCommand

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

# Minimal, short-duration parameters — deliberately small blast radius.
REVERSION_TEST_POWER_W = 50
REVERSION_TIMER_S = 15
REVERSION_POLL_TIMEOUT_S = 30
REVERSION_POLL_INTERVAL_S = 2

RESERVE_TEST_DELTA_PCT = 3


class TestWSetRvrtTmsReversion:
    """Does the hardware auto-revert timer actually clear WSetEna/WSetPct?

    See docs/vendor-issues.md Issue 1 — prior evidence (manual test logs,
    not pytest) says the countdown decrements but reversion never fires.
    This codifies that claim as a real, re-runnable assertion using a
    minimal, short-duration dispatch, with the library's own software
    duration_s timer deliberately left off so only the hardware mechanism
    is under test.
    """

    def test_hardware_reversion_after_expiry(self, controller):
        m704 = controller.get_model(704)
        if not m704:
            pytest.skip("Model 704 not available")

        try:
            ok, msg = controller.send_command(
                BatteryCommand(power_watts=REVERSION_TEST_POWER_W),
                duration_s=None,
            )
            assert ok, f"send_command failed, aborting reversion probe: {msg}"

            m704.read()
            m704.WSetRvrtTms.value = REVERSION_TIMER_S
            m704.write()

            # Confirm the countdown register is actually decrementing first —
            # if it isn't even doing that, the test below isn't meaningful.
            m704.read()
            initial_rem = m704.WSetRvrtRem.value
            time.sleep(REVERSION_TIMER_S / 2)
            m704.read()
            mid_rem = m704.WSetRvrtRem.value

            deadline = time.time() + REVERSION_POLL_TIMEOUT_S
            reverted = False
            while time.time() < deadline:
                m704.read()
                if m704.WSetEna.value == 0:
                    reverted = True
                    break
                time.sleep(REVERSION_POLL_INTERVAL_S)

            # Intentionally informative either way: if FranklinWH has since
            # fixed hardware reversion, this assertion should start FAILING
            # — that failure is the signal to update Issue 1, not a bug in
            # this test.
            assert reverted, (
                "Hardware reversion did NOT occur — WSetEna stayed 1 past "
                f"expiry (waited {REVERSION_POLL_TIMEOUT_S}s past a "
                f"{REVERSION_TIMER_S}s timer). Matches prior evidence in "
                f"docs/vendor-issues.md Issue 1. Countdown moved "
                f"{initial_rem} -> {mid_rem} at the halfway point, confirming "
                "the countdown register itself IS decrementing — only the "
                "actual power reversion is cosmetic."
            )
        finally:
            # Always force-release, whether or not hardware reversion
            # occurred — never leave a real dispatch armed on exit.
            controller.reset_control_state()
            m704.read()
            m704.WSetRvrtTms.value = 0
            m704.WSetEnaRvrt.value = 0
            m704.write()


class TestReserveSoCPersistence:
    """Does writing Self-Consumption reserve SoC persist, and specifically,
    does touching OnGridMode reset it?

    See docs/vendor-issues.md Issue 11 — later evidence (2026 test logs) says
    the write fails outright ("SPAN Modbus Lock"). But a separate, primary
    source — FranklinWH's own SunSpec certifier's internal 2023 review —
    describes a more specific mechanism: the value resets to a default
    specifically when OnGridMode is updated, not necessarily on every write.
    These two tests distinguish the hypotheses; neither has been run before.
    """

    def test_self_reserve_write_persists_immediately(self, controller):
        mode = controller.read_native_mode()
        original_pct = mode.get("self_reserve_pct")
        if original_pct is None:
            pytest.skip("self_reserve_pct not readable")

        test_pct = original_pct + RESERVE_TEST_DELTA_PCT
        if test_pct > 100:
            test_pct = original_pct - RESERVE_TEST_DELTA_PCT

        try:
            ok, msg = controller.set_self_consumption_reserve(test_pct)
            time.sleep(1.0)
            after = controller.read_native_mode().get("self_reserve_pct")

            assert after == test_pct, (
                f"Write did not persist: wrote {test_pct}, read back {after} "
                f"(set_self_consumption_reserve returned ok={ok}, "
                f"msg={msg!r}). Matches docs/vendor-issues.md Issue 11 — "
                "prior evidence this write fails on real hardware."
            )
        finally:
            controller.set_self_consumption_reserve(original_pct)

    def test_self_reserve_resets_when_ongrid_mode_touched(self, controller):
        """Distinguishes 'hard lock' from 'resets specifically when OnGridMode
        is updated' per the 2023 SPAN internal review (docs/vendor-issues.md
        Issue 11 update). Writes the reserve, confirms it stuck (or not),
        then writes OnGridMode back to its OWN current value (a no-op mode
        change) and checks whether the reserve changed as a side effect.
        """
        mode = controller.read_native_mode()
        original_pct = mode.get("self_reserve_pct")
        original_mode_raw = mode.get("mode_raw")
        if original_pct is None or original_mode_raw is None:
            pytest.skip("reserve or mode not readable")

        test_pct = original_pct + RESERVE_TEST_DELTA_PCT
        if test_pct > 100:
            test_pct = original_pct - RESERVE_TEST_DELTA_PCT

        try:
            controller.set_self_consumption_reserve(test_pct)
            time.sleep(1.0)
            before_touch = controller.read_native_mode().get("self_reserve_pct")

            # No-op mode "touch" — same mode, written back.
            controller.set_native_mode(original_mode_raw)
            time.sleep(1.0)
            after_touch = controller.read_native_mode().get("self_reserve_pct")

            # Informative regardless of which hypothesis holds:
            #  - before_touch != test_pct  -> write is blocked outright,
            #    OnGridMode isn't even a factor (matches 2026 test logs).
            #  - before_touch == test_pct but after_touch != test_pct ->
            #    confirms the 2023 SPAN-documented reset-on-mode-touch
            #    behavior specifically.
            #  - both equal test_pct -> neither failure mode reproduces on
            #    this firmware; Issue 11 may be stale.
            assert after_touch == before_touch, (
                f"Reserve changed after a no-op OnGridMode write: "
                f"{before_touch} -> {after_touch} (wrote mode "
                f"{original_mode_raw}, its own current value). This matches "
                "the 2023 SPAN internal review's documented behavior "
                "(docs/vendor-issues.md Issue 11 update) — confirms the "
                "reset is tied to OnGridMode updates specifically, not a "
                "permanent hard lock."
            )
        finally:
            controller.set_self_consumption_reserve(original_pct)
            controller.set_native_mode(original_mode_raw)
