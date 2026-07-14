#!/usr/bin/env python3
"""Diagnostic: does a genuinely ATOMIC Modbus write -- one function-code-16
transaction covering OnGrid Mode + both reserve registers in a single
request -- avoid the reset-to-default behaviour FranklinWH/SPAN's 2023
internal PICS review documents for 15508/15509?

This is the Modbus-native counterpart to tools/test_cloud_soc_persistence.py
(which tests the same "set together" hypothesis via the Cloud API instead).
Context: docs/vendor-issues.md, Issue 11.

Why this needs a standalone script rather than the bridge's own Sequencer:
SunSpecSequencer.execute_writes() (franklinwh-modbus) issues one function-
code-6 transaction per register, sequentially -- even when a step lists
several registers -- and silently SKIPS a write whose target already
matches the current value (no wire traffic at all). That means the
Sequencer literally cannot express "keep the current mode value, but write
it anyway as part of one atomic block" -- a same-value write never goes out.
This script uses pymodbus's write_registers() directly, which sends a real
function-code-16 request regardless of whether any individual value in the
block is unchanged, so it can test:
  - "switch mode + set reserve, atomically" (mode value differs), and
  - "keep the SAME mode, set reserve, atomically" (mode value identical --
    genuinely untestable via the Sequencer, testable here)
Filed as a Sequencer feature request: franklinwh-modbus#11 (batch_write /
batch_read support) -- this script exists so the hypothesis doesn't have to
wait for that to land.

Safety -- concurrent Modbus access: this aGate firmware has a documented
history of 0xFFFF register corruption from overlapping Modbus sessions (see
poller.py's own comments on why it stopped duplicate-reading 15500-15509).
By default this script stops the bridge's gateway before writing and
restarts it afterward (via the bridge's own REST API) so the regular poll
loop can't race this script's raw socket. Pass --no-bridge-coordination
only if the bridge isn't running against this device at all.

Usage:
    # Keep the current mode, just test whether the reserve write holds:
    python tools/test_atomic_block_write.py --gateway-ip 192.168.1.100 --self-soc 15

    # Switch mode (oldIndex 1=Backup, 2=Self-Consumption, 3=TOU) AND reserve
    # in the same atomic write:
    python tools/test_atomic_block_write.py --gateway-ip 192.168.1.100 \
        --self-soc 15 --mode 2

Requires: pymodbus (already a bridge dependency).

Safety: this changes the real battery's operating mode and reserve SOC on
your live system. It refuses to run without --yes or an interactive
confirmation, and restores the original mode + reserves afterward (best-
effort -- verify manually if the script is interrupted).
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

REG_ONGRID_MODE = 15507  # Modbus oldIndex numbering: 1=Backup, 2=Self, 3=TOU
REG_SELF_RESERVE = 15508
REG_TOU_RESERVE = 15509


def read_block(ip: str, unit: int) -> tuple[int, int, int]:
    """Returns (mode, self_reserve_pct, tou_reserve_pct) via one read."""
    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(ip, port=502)
    try:
        if not client.connect():
            raise RuntimeError(f"Could not connect to {ip}:502")
        rr = client.read_holding_registers(REG_ONGRID_MODE, count=3, device_id=unit)
        if rr.isError():
            raise RuntimeError(f"Modbus read failed at {REG_ONGRID_MODE}: {rr}")
        mode, self_pct, tou_pct = rr.registers
        return mode, self_pct, tou_pct
    finally:
        client.close()


def fmt_block(mode: int, self_pct: int, tou_pct: int) -> str:
    return f"OnGridMode(oldIndex)={mode}  SelfReserve={self_pct}%  TOUReserve={tou_pct}%"


def atomic_write_block(ip: str, unit: int, mode: int, self_pct: int, tou_pct: int) -> None:
    """One function-code-16 transaction covering all three registers."""
    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(ip, port=502)
    try:
        if not client.connect():
            raise RuntimeError(f"Could not connect to {ip}:502")
        rr = client.write_registers(
            REG_ONGRID_MODE, [mode, self_pct, tou_pct], device_id=unit
        )
        if rr.isError():
            raise RuntimeError(f"Atomic block write failed: {rr}")
    finally:
        client.close()


def _bridge_call(bridge_url: str, path: str) -> bool:
    req = urllib.request.Request(f"{bridge_url}{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.URLError as exc:
        print(f"  WARNING: bridge call {path} failed: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-ip", required=True)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument(
        "--self-soc", type=int, required=True, help="Target Self-Consumption reserve %%"
    )
    parser.add_argument(
        "--tou-soc", type=int, default=None,
        help="Target TOU reserve %% (default: keep current value)",
    )
    parser.add_argument(
        "--mode", type=int, default=None, choices=[1, 2, 3],
        help="Target OnGrid Mode, oldIndex (1=Backup 2=Self 3=TOU). "
             "Default: keep the current mode (still sent atomically).",
    )
    parser.add_argument(
        "--settle-delay-s", type=float, default=10.0, help="Delay before the second readback"
    )
    parser.add_argument(
        "--bridge-url", default="http://localhost:8199",
        help="Bridge base URL, used to pause/resume polling around the test",
    )
    parser.add_argument(
        "--bridge-gateway-id", default="default",
        help="Gateway ID to stop/start on the bridge during the test",
    )
    parser.add_argument(
        "--no-bridge-coordination", action="store_true",
        help="Skip stopping/restarting the bridge's gateway (only safe if "
             "the bridge isn't polling this device at all)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation prompt"
    )
    args = parser.parse_args()

    print(f"=== Reading baseline state from {args.gateway_ip} (Modbus) ===")
    mode_before, self_before, tou_before = read_block(args.gateway_ip, args.unit)
    print(f"  {fmt_block(mode_before, self_before, tou_before)}")

    target_mode = args.mode if args.mode is not None else mode_before
    target_tou = args.tou_soc if args.tou_soc is not None else tou_before
    mode_note = "unchanged" if target_mode == mode_before else "SWITCHING"
    print(f"\nTarget (one atomic write): mode={target_mode} ({mode_note}), "
          f"self={args.self_soc}%, tou={target_tou}%")

    if self_before == args.self_soc and target_mode == mode_before and target_tou == tou_before:
        print("NOTE: target state is identical to current state -- pick a "
              "different value so a real transition happens.")
        return 1

    if not args.yes:
        resp = input(
            f"\nThis sends ONE atomic Modbus write (function code 16) to your "
            f"REAL battery at {args.gateway_ip}, then restores the original "
            f"values afterward. Continue? [y/N] "
        )
        if resp.strip().lower() != "y":
            print("Aborted.")
            return 1

    stopped = False
    if not args.no_bridge_coordination:
        print(f"\n=== Pausing bridge polling for gateway '{args.bridge_gateway_id}' ===")
        stopped = _bridge_call(
            args.bridge_url, f"/api/gateways/{args.bridge_gateway_id}/stop"
        )
        if not stopped:
            print("  could not pause -- proceeding anyway, at higher risk of a "
                  "concurrent-access glitch")
        else:
            print("  paused")

    try:
        print(f"\n=== Atomic write: mode={target_mode}, "
              f"self={args.self_soc}%, tou={target_tou}% ===")
        atomic_write_block(args.gateway_ip, args.unit, target_mode, args.self_soc, target_tou)

        print("\n=== Immediate Modbus readback ===")
        mode_immediate, self_immediate, tou_immediate = read_block(args.gateway_ip, args.unit)
        print(f"  {fmt_block(mode_immediate, self_immediate, tou_immediate)}")
        immediate_ok = self_immediate == args.self_soc
        print(f"  {'PERSISTED' if immediate_ok else 'DID NOT PERSIST'} immediately")

        print(f"\n=== Waiting {args.settle_delay_s}s to check for a delayed reset ===")
        time.sleep(args.settle_delay_s)
        mode_delayed, self_delayed, tou_delayed = read_block(args.gateway_ip, args.unit)
        print(f"  {fmt_block(mode_delayed, self_delayed, tou_delayed)}")
        if self_delayed == args.self_soc:
            delayed_verdict = "STILL PERSISTED"
        elif immediate_ok:
            delayed_verdict = "RESET after the delay (immediate write held, then reverted)"
        else:
            delayed_verdict = "DID NOT PERSIST (consistent with immediate result)"
        print(f"  {delayed_verdict}")

        print(f"\n=== Restoring original block (mode={mode_before}, "
              f"self={self_before}%, tou={tou_before}%) ===")
        atomic_write_block(args.gateway_ip, args.unit, mode_before, self_before, tou_before)
        mode_final, self_final, tou_final = read_block(args.gateway_ip, args.unit)
        print(f"  Post-restore: {fmt_block(mode_final, self_final, tou_final)}")
        if (mode_final, self_final, tou_final) != (mode_before, self_before, tou_before):
            print("  WARNING: post-restore state does not match the "
                  "original baseline -- verify manually.")

        print("\n=== Summary ===")
        print(f"  Before:                {fmt_block(mode_before, self_before, tou_before)}")
        ok_label = "ok" if immediate_ok else "no"
        print(f"  Immediately after:     "
              f"{fmt_block(mode_immediate, self_immediate, tou_immediate)}  ({ok_label})")
        print(f"  After {args.settle_delay_s:>5.1f}s:         "
              f"{fmt_block(mode_delayed, self_delayed, tou_delayed)}  ({delayed_verdict})")
        print(f"  After restore:         {fmt_block(mode_final, self_final, tou_final)}")
    finally:
        if stopped:
            print(f"\n=== Resuming bridge polling for gateway "
                  f"'{args.bridge_gateway_id}' ===")
            resumed = _bridge_call(
                args.bridge_url, f"/api/gateways/{args.bridge_gateway_id}/start"
            )
            if resumed:
                print("  resumed")
            else:
                print("  FAILED TO RESUME -- restart it manually via Settings")

    return 0


if __name__ == "__main__":
    sys.exit(main())
