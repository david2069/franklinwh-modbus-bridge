#!/usr/bin/env python3
"""Diagnostic: classify undocumented FranklinWH extension registers as
Modbus-protocol read-only vs write-capable, WITHOUT changing any device
state -- by writing each register's own currently-observed value back to
itself and checking whether the device ACKs the write or returns a Modbus
exception (illegal address/function).

Why this is safe: same value in, same value out. If a register is
functionally inert, nothing changes. If it's genuinely read-only, the
device should reject the write outright (an explicit protocol-level
signal) rather than silently discarding a real value change -- so this
classifies writability without ever introducing an actual state change.

Caveat: this only tests "does the protocol accept a write to this
address," not "would a DIFFERENT value have any functional effect" or
"this specific write is truly inert internally." A small non-zero risk
remains that some unknown register is command-triggered (any write,
regardless of value, causes an action) rather than value-holding -- this
script writes ONE register at a time with a pause between each, and
prints its full plan before touching hardware, specifically so it can be
watched/aborted mid-run.

Context: docs/reference/register-writability-probe.md records the
methodology and results of running this. Targets the undocumented
15000-15039 and 16000-16002 blocks documented (reads only) in
docs/vendor-issues.md Issue 8.

Usage:
    python tools/probe_register_writability.py --gateway-ip 192.168.1.100

Requires: pymodbus (already a bridge dependency).

Safety: pauses the bridge's own polling for the target gateway (via its
REST API) before probing and resumes it afterward, to avoid the documented
concurrent-Modbus-access corruption risk on this firmware (see
poller.py's own comments on why it stopped duplicate-reading 15500-15509).
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

DEFAULT_RANGES = [(15000, 40), (16000, 3)]


def _bridge_call(bridge_url: str, path: str) -> bool:
    req = urllib.request.Request(f"{bridge_url}{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.URLError as exc:
        print(f"  WARNING: bridge call {path} failed: {exc}")
        return False


def _ensure_connected(client) -> bool:
    if getattr(client, "connected", False):
        return True
    return client.connect()


def probe_register(client, unit: int, addr: int) -> dict:
    """Read a register, write its own value back, re-read, classify."""
    if not _ensure_connected(client):
        return {"addr": addr, "value": None, "writable": None, "note": "connect failed"}

    try:
        rr = client.read_holding_registers(addr, count=1, device_id=unit)
    except Exception as exc:  # noqa: BLE001 -- a bad register must not abort the whole probe
        return {"addr": addr, "value": None, "writable": None, "note": f"read raised: {exc}"}
    if rr.isError():
        return {"addr": addr, "value": None, "writable": None, "note": f"read failed: {rr}"}
    value = rr.registers[0]

    try:
        wr = client.write_register(addr, value, device_id=unit)
    except Exception as exc:  # noqa: BLE001
        return {"addr": addr, "value": value, "writable": False, "note": f"write raised: {exc}"}
    if wr.isError():
        return {"addr": addr, "value": value, "writable": False, "note": str(wr)}

    try:
        rr2 = client.read_holding_registers(addr, count=1, device_id=unit)
        unchanged = (not rr2.isError()) and rr2.registers[0] == value
    except Exception as exc:  # noqa: BLE001
        return {"addr": addr, "value": value, "writable": True,
                "note": f"post-write read raised: {exc}"}
    note = "" if unchanged else (
        f"UNEXPECTED: value changed to "
        f"{rr2.registers[0] if not rr2.isError() else '??'} after a same-value write"
    )
    return {"addr": addr, "value": value, "writable": True, "note": note}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-ip", required=True)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument(
        "--pause-s", type=float, default=0.3, help="Delay between registers"
    )
    parser.add_argument(
        "--bridge-url", default="http://localhost:8199",
        help="Bridge base URL, used to pause/resume polling around the probe",
    )
    parser.add_argument("--bridge-gateway-id", default="default")
    parser.add_argument(
        "--no-bridge-coordination", action="store_true",
        help="Skip stopping/restarting the bridge's gateway (only safe if "
             "the bridge isn't polling this device at all)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation prompt"
    )
    args = parser.parse_args()

    addrs: list[int] = []
    for start, count in DEFAULT_RANGES:
        addrs.extend(range(start, start + count))

    print(f"=== Plan: probe {len(addrs)} registers on {args.gateway_ip}, "
          f"one write-back at a time ===")
    for start, count in DEFAULT_RANGES:
        print(f"  {start}-{start + count - 1} ({count} registers)")

    if not args.yes:
        resp = input(
            f"\nThis writes each register's OWN current value back to itself, "
            f"one at a time, against your REAL device at {args.gateway_ip}. No "
            f"functional state change is intended. Continue? [y/N] "
        )
        if resp.strip().lower() != "y":
            print("Aborted.")
            return 1

    stopped = False
    if not args.no_bridge_coordination:
        print(f"\n=== Pausing bridge polling for gateway '{args.bridge_gateway_id}' ===")
        stopped = _bridge_call(args.bridge_url, f"/api/gateways/{args.bridge_gateway_id}/stop")
        if stopped:
            print("  paused")
        else:
            print("  could not pause -- proceeding anyway, at higher risk of a "
                  "concurrent-access glitch")

    results: list[dict] = []
    try:
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(args.gateway_ip, port=502)
        if not client.connect():
            print(f"ERROR: could not connect to {args.gateway_ip}:502")
            return 1
        try:
            print()
            for addr in addrs:
                r = probe_register(client, args.unit, addr)
                results.append(r)
                if r["writable"] is True:
                    status = "WRITABLE"
                elif r["writable"] is False:
                    status = "READ-ONLY"
                else:
                    status = "READ-FAILED"
                extra = f"  {r['note']}" if r["note"] else ""
                print(f"  {addr}: value={r['value']}  {status}{extra}")
                time.sleep(args.pause_s)
        finally:
            client.close()
    finally:
        if stopped:
            print(f"\n=== Resuming bridge polling for gateway '{args.bridge_gateway_id}' ===")
            resumed = _bridge_call(args.bridge_url, f"/api/gateways/{args.bridge_gateway_id}/start")
            if resumed:
                print("  resumed")
            else:
                print("  FAILED TO RESUME -- restart it manually via Settings")

    writable = [r for r in results if r["writable"] is True]
    readonly = [r for r in results if r["writable"] is False]
    failed = [r for r in results if r["writable"] is None]
    print(f"\n=== Summary: {len(writable)} writable, {len(readonly)} read-only, "
          f"{len(failed)} read-failed (of {len(results)}) ===")
    if writable:
        print("  Writable:  ", ", ".join(str(r["addr"]) for r in writable))
    if readonly:
        print("  Read-only: ", ", ".join(str(r["addr"]) for r in readonly))
    if failed:
        print("  Read-failed:", ", ".join(str(r["addr"]) for r in failed))

    anomalies = [r for r in results if r["writable"] and r["note"]]
    if anomalies:
        print(f"\n  ANOMALIES (value changed unexpectedly on a same-value write): "
              f"{[r['addr'] for r in anomalies]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
