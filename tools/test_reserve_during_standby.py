#!/usr/bin/env python3
"""Diagnostic: does a reserve SOC (or OnGridMode) write persist while the
bridge's own local Modbus control (Force Standby, WSetEna=1) is already
engaged -- as opposed to the idle/native state every prior write test has
used?

Hypothesis (user's): SPAN's "resets to a default when OnGridMode is
updated" note might describe behaviour specific to the NATIVE control
loop, which is suspended while local/VPP override holds WSetEna=1 (see
docs/scheduling-and-orchestration-design.md's "VPP overrides native TOU
with no mode-switch"). If the reset is driven by that native loop, a
write attempted during an active local-control window might behave
differently. This has never been tested (confirmed by searching
franklinwh-modbus's test-results logs) -- prior write-testing was always
done idle/native (WSetEna=0).

Force Standby (not Force Charge/Discharge) is used deliberately: it's 0W,
the safest possible local-control state, while still setting WSetEna=1.

Why this goes entirely through the bridge's own REST/Sequencer API rather
than a raw parallel Modbus connection: GatewayInstance.stop() explicitly
releases any active command as part of shutdown (command_handler.py:621-
629), so "stop the gateway to avoid concurrent access" (the pattern used
in this project's other diagnostic scripts) would undo the very state
this test needs to hold. Routing everything through the bridge's existing
single Modbus session (already serialised against its own poller via
modbus_lock) avoids the documented 0xFFFF concurrent-access corruption
risk without releasing Standby.

Context: docs/vendor-issues.md Issue 11.

Usage:
    python tools/test_reserve_during_standby.py --bridge-gateway-id default

Requires: nothing beyond the standard library -- talks to the bridge's
own REST API only, never touches Modbus directly.

Safety: engages Force Standby (0W, no real charge/discharge) on your real
battery, runs three write/verify/settle/restore sub-tests, then releases
control back to native and restores the original mode/reserve values.
Refuses to run without --yes or an interactive confirmation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

REG_ONGRID_MODE = "15507"
REG_SELF_RESERVE = "15508"
REG_TOU_RESERVE = "15509"


def _get(bridge_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{bridge_url}{path}", timeout=15) as resp:
        return json.loads(resp.read())


def _post(bridge_url: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        f"{bridge_url}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_points(bridge_url: str) -> dict:
    return _get(bridge_url, "/api/points").get("points", {})


def command(bridge_url: str, gw_id: str, slug: str, value: str) -> dict:
    return _post(bridge_url, f"/api/gateways/{gw_id}/command", {"slug": slug, "value": value})


def run_sequence(bridge_url: str, gw_id: str, steps: list[dict]) -> dict:
    return _post(bridge_url, "/api/sequence/execute", {"sequence": steps, "gateway_id": gw_id})


def wait_for_wseten(bridge_url: str, target: int, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pts = get_points(bridge_url)
        if int(pts.get("704.WSetEna", -1)) == target:
            return True
        time.sleep(0.5)
    return False


def sub_test(
    bridge_url: str, gw_id: str, name: str, writes: dict, restore: dict, settle_s: float
) -> None:
    print(f"\n--- Sub-test: {name} ---")
    write_steps = [
        {"step": f"{name}: write", "writes": writes, "verify": True, "verify_timeout_ms": 3000},
    ]
    result = run_sequence(bridge_url, gw_id, write_steps)
    print(f"  write result: ok={result.get('ok')}")
    for line in result.get("output", []):
        print(f"    {line}")

    pts = get_points(bridge_url)
    print(f"  immediate: ongrid_mode={pts.get('ongrid_mode')} "
          f"self_reserve={pts.get('self_reserve_pct')} tou_reserve={pts.get('tou_reserve_pct')} "
          f"WSetEna={pts.get('704.WSetEna')}")

    print(f"  settling {settle_s}s...")
    time.sleep(settle_s)
    pts2 = get_points(bridge_url)
    print(f"  after settle: ongrid_mode={pts2.get('ongrid_mode')} "
          f"self_reserve={pts2.get('self_reserve_pct')} tou_reserve={pts2.get('tou_reserve_pct')} "
          f"WSetEna={pts2.get('704.WSetEna')}")

    restore_steps = [
        {"step": f"{name}: restore", "writes": restore, "verify": True, "verify_timeout_ms": 3000},
    ]
    result = run_sequence(bridge_url, gw_id, restore_steps)
    print(f"  restore result: ok={result.get('ok')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bridge-url", default="http://localhost:8199")
    parser.add_argument("--bridge-gateway-id", default="default")
    parser.add_argument(
        "--settle-s", type=float, default=8.0,
        help="Delay between write and re-check, per sub-test",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation prompt"
    )
    args = parser.parse_args()
    gw = args.bridge_gateway_id
    url = args.bridge_url

    print("=== Reading baseline via bridge REST API ===")
    pts = get_points(url)
    mode_before = int(pts["ongrid_mode"])
    self_before = int(pts["self_reserve_pct"])
    tou_before = int(pts["tou_reserve_pct"])
    wseten_before = int(pts["704.WSetEna"])
    print(f"  ongrid_mode={mode_before}  self_reserve={self_before}%  "
          f"tou_reserve={tou_before}%  WSetEna={wseten_before}")

    if wseten_before != 0:
        print("ERROR: WSetEna is already 1 -- something else already has local control. Aborting.")
        return 1

    self_target = self_before + 5 if self_before < 90 else self_before - 5
    mode_target = 2 if mode_before != 2 else 3  # toggle Self-Consumption <-> TOU only

    print("\nPlan (all while Force Standby / WSetEna=1 is engaged):")
    print(f"  1. reserve-only:  {REG_SELF_RESERVE} {self_before} -> {self_target} -> restore")
    print(f"  2. mode-only:     {REG_ONGRID_MODE} {mode_before} -> {mode_target} -> restore")
    print("  3. both together (sequential, same Sequencer call) -> restore")

    if not args.yes:
        resp = input(
            f"\nThis engages Force Standby (0W, no real charge/discharge) on your "
            f"REAL battery via gateway '{gw}', runs the three sub-tests above, "
            f"then releases control and restores the original values. Continue? [y/N] "
        )
        if resp.strip().lower() != "y":
            print("Aborted.")
            return 1

    print(f"\n=== Engaging Force Standby on gateway '{gw}' ===")
    cmd_result = command(url, gw, "battery_command", "Force Standby")
    print(f"  command result: {cmd_result}")

    if not wait_for_wseten(url, 1):
        print("ERROR: WSetEna did not reach 1 within timeout -- "
              "aborting without touching reserves.")
        return 1
    print("  WSetEna=1 confirmed -- local control engaged.")

    try:
        sub_test(
            url, gw, "reserve-only",
            writes={REG_SELF_RESERVE: self_target},
            restore={REG_SELF_RESERVE: self_before},
            settle_s=args.settle_s,
        )
        sub_test(
            url, gw, "mode-only",
            writes={REG_ONGRID_MODE: mode_target},
            restore={REG_ONGRID_MODE: mode_before},
            settle_s=args.settle_s,
        )
        sub_test(
            url, gw, "mode+reserve together (sequential)",
            writes={REG_ONGRID_MODE: mode_target, REG_SELF_RESERVE: self_target},
            restore={REG_ONGRID_MODE: mode_before, REG_SELF_RESERVE: self_before},
            settle_s=args.settle_s,
        )
    finally:
        print(f"\n=== Releasing local control on gateway '{gw}' ===")
        rel_result = command(url, gw, "battery_command", "Release")
        print(f"  release result: {rel_result}")
        wait_for_wseten(url, 0)

        pts_final = get_points(url)
        print("\n=== Final state ===")
        print(f"  ongrid_mode={pts_final.get('ongrid_mode')}  "
              f"self_reserve={pts_final.get('self_reserve_pct')}%  "
              f"tou_reserve={pts_final.get('tou_reserve_pct')}%  "
              f"WSetEna={pts_final.get('704.WSetEna')}")
        if (
            int(pts_final.get("ongrid_mode", -1)) != mode_before
            or int(pts_final.get("self_reserve_pct", -1)) != self_before
        ):
            print("  WARNING: final state does not match the original baseline -- verify manually.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"ERROR: bridge REST call failed: {exc}")
        sys.exit(1)
