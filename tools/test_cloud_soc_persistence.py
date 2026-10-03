#!/usr/bin/env python3
"""Diagnostic: does franklinwh-cloud's set_mode(requestedSOC=...) actually
persist the reserve SOC on real hardware, or does it just accept the
request without a confirmed on-device effect?

Closes a readback gap in franklinwh-cloud's own test suite: its one live
test (tests/test_live_mode.py) asserts set_mode(requestedSOC=...) returns
True and that `workMode` changed, but never re-reads the SOC value itself.
This script does the missing readback, via Modbus (15508 Self-Consumption
Reserve), immediately after the call and again after a settle delay, to
catch either an immediate no-op or a delayed reset back to default.

Context: docs/vendor-issues.md, Issue 11 (franklinwh-modbus-bridge repo),
2026-07-15 update. SPAN's own 2023 internal conformance review documents
15508/15509 "resets to a default when OnGridMode is updated" -- this script
tests whether pairing the SOC with a same-call mode set (as the Cloud API's
set_mode() does) avoids that reset, unlike a bare Modbus write to 15508.

This is a standalone diagnostic script, not part of either shipped package
(franklinwh-modbus-bridge or franklinwh-cloud) -- it deliberately reaches
into both directly rather than adding a permanent dependency either way.

Usage:
    python tools/test_cloud_soc_persistence.py --gateway-ip 192.168.1.100

Requires:
    - franklinwh_cloud importable (pip install -e ~/dev/franklinwh-cloud)
    - Cloud credentials loadable via franklinwh_cloud.cli.load_credentials()
    - pymodbus (already a bridge dependency)

Safety:
    - This changes the real battery's operating mode and reserve SOC on your
      live system. It refuses to run without --yes or an interactive
      confirmation, and restores the original mode + reserve afterward
      (best-effort -- verify manually if the script is interrupted).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

REG_SELF_RESERVE = 15508
REG_ONGRID_MODE = 15507  # Modbus oldIndex numbering: 1=Backup, 2=Self, 3=TOU


def read_register(ip: str, unit: int, addr: int) -> int:
    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(ip, port=502)
    try:
        if not client.connect():
            raise RuntimeError(f"Could not connect to {ip}:502")
        rr = client.read_holding_registers(addr, count=1, device_id=unit)
        if rr.isError():
            raise RuntimeError(f"Modbus read failed at {addr}: {rr}")
        return rr.registers[0]
    finally:
        client.close()


def read_state(ip: str, unit: int) -> tuple[int, int]:
    """Returns (ongrid_mode, self_reserve_pct)."""
    return read_register(ip, unit, REG_ONGRID_MODE), read_register(ip, unit, REG_SELF_RESERVE)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-ip", required=True)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument(
        "--target-soc", type=int, default=15, help="Reserve SOC to test-write (default: 15)"
    )
    parser.add_argument(
        "--settle-delay-s", type=float, default=10.0, help="Delay before the second readback"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation prompt"
    )
    args = parser.parse_args()

    try:
        from franklinwh_cloud.cli import load_credentials
        from franklinwh_cloud.client import Client, TokenFetcher
        from franklinwh_cloud.const.modes import MODBUS_TO_CLOUD_MODE, SELF_CONSUMPTION
    except ImportError as exc:
        print(f"ERROR: franklinwh_cloud not importable ({exc}). "
              f"pip install -e ~/dev/franklinwh-cloud")
        return 1

    email, password, gateway = load_credentials()
    if not (email and password and gateway):
        print("ERROR: no franklinwh-cloud credentials found "
              "(see franklinwh_cloud.cli.load_credentials)")
        return 1

    print(f"=== Reading baseline state from {args.gateway_ip} (Modbus) ===")
    mode_before, self_before = read_state(args.gateway_ip, args.unit)
    print(f"  OnGridMode(oldIndex)={mode_before}  SelfReserve={self_before}%")

    if self_before == args.target_soc:
        print(f"NOTE: current Self-Consumption reserve is already {args.target_soc}% "
              f"-- pick a different --target-soc so a real transition happens.")
        return 1

    if mode_before not in MODBUS_TO_CLOUD_MODE:
        print(f"ERROR: unrecognised OnGridMode value {mode_before} "
              f"-- refusing to guess a restore mode.")
        return 1
    cloud_mode_before = MODBUS_TO_CLOUD_MODE[mode_before]

    if not args.yes:
        resp = input(
            f"\nThis calls franklinwh-cloud set_mode(SELF_CONSUMPTION, "
            f"requestedSOC={args.target_soc}) against your REAL battery at "
            f"{args.gateway_ip}, then restores the original mode/reserve "
            f"afterward. Continue? [y/N] "
        )
        if resp.strip().lower() != "y":
            print("Aborted.")
            return 1

    fetcher = TokenFetcher(email, password)
    client = Client(fetcher, gateway)

    print(f"\n=== Calling set_mode(SELF_CONSUMPTION, "
          f"requestedSOC={args.target_soc}) via Cloud API ===")
    ok = await client.set_mode(
        requestedOperatingMode=SELF_CONSUMPTION,
        requestedSOC=args.target_soc,
        reqbackupForeverFlag=None,
        reqnextWorkMode=None,
        reqdurationMinutes=None,
    )
    print(f"  HTTP result: {ok}")

    print("\n=== Immediate Modbus readback ===")
    mode_immediate, self_immediate = read_state(args.gateway_ip, args.unit)
    print(f"  OnGridMode(oldIndex)={mode_immediate}  SelfReserve={self_immediate}%")
    immediate_ok = self_immediate == args.target_soc
    print(f"  {'PERSISTED' if immediate_ok else 'DID NOT PERSIST'} immediately")

    print(f"\n=== Waiting {args.settle_delay_s}s to check for a delayed reset ===")
    time.sleep(args.settle_delay_s)
    mode_delayed, self_delayed = read_state(args.gateway_ip, args.unit)
    print(f"  OnGridMode(oldIndex)={mode_delayed}  SelfReserve={self_delayed}%")
    if self_delayed == args.target_soc:
        delayed_verdict = "STILL PERSISTED"
    elif immediate_ok:
        delayed_verdict = "RESET after the delay (immediate write held, then reverted)"
    else:
        delayed_verdict = "DID NOT PERSIST (consistent with immediate result)"
    print(f"  {delayed_verdict}")

    print(f"\n=== Restoring original values (mode={mode_before}, reserve={self_before}%) ===")
    restored = await client.set_mode(
        requestedOperatingMode=cloud_mode_before,
        requestedSOC=self_before,
        reqbackupForeverFlag=None,
        reqnextWorkMode=None,
        reqdurationMinutes=None,
    )
    print(f"  Restore HTTP result: {restored}")
    mode_final, self_final = read_state(args.gateway_ip, args.unit)
    print(f"  Post-restore: OnGridMode(oldIndex)={mode_final}  SelfReserve={self_final}%")
    if mode_final != mode_before or self_final != self_before:
        print("  WARNING: post-restore state does not match the original "
              "baseline -- verify manually.")

    print("\n=== Summary ===")
    print(f"  Before:                mode={mode_before}  self={self_before}%")
    ok_label = "ok" if immediate_ok else "no"
    print(f"  Immediately after:     mode={mode_immediate}  "
          f"self={self_immediate}%  ({ok_label})")
    print(f"  After {args.settle_delay_s:>5.1f}s:         mode={mode_delayed}  "
          f"self={self_delayed}%  ({delayed_verdict})")
    print(f"  After restore:         mode={mode_final}  self={self_final}%")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
