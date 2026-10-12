# Dispatch reliability and the Modbus connection: design

**Status:** DRAFT for owner review (2026-10-12). No code until this is
approved, the aGate measurements in §6.1 have run, and the test rig in §6.2
exists.
**Issues:** #25, #26, #56.
**Owner decisions needed:** §9.

## 1. Problem

A scheduled force discharge has silently failed three times in eight days. Each
time, the bridge reported success.

| Date | Issue | What the bridge did | What happened |
|---|---|---|---|
| Sun 2026-10-04 | #25 | The Modbus socket broke (`EPIPE`) and was kept for about 23 h. The 18:00 entry was gated on an unknown `grid.connected` and never fired. A cached SoC was shown as live. | No discharge. No alert. |
| Wed 2026-10-07 | #26 | The write was acknowledged: WSetEna 1, WSetPct 100. The audit said "ok". | The battery stayed at 0 W for 38 min, until the owner released and re-sent by hand. |
| Mon 2026-10-12 | #56 | The write failed with `Response timeout` and WSetEna 0. The audit still said "ok"; the command was marked active and never retried. | No discharge for the whole 90-min window. No alert. |

**Two separate things are wrong:**
- **(A)** The bridge does not know whether a dispatch took effect, and it reports success regardless. This is proven, from the code and the records.
- **(B)** The Modbus link to the aGate is unhealthy: thousands of timeouts and reconnects a day. The cause is unknown.

**(A) turns any glitch into a lost evening, so it is fixed first.** It does not
depend on the cause of (B).

## 2. What is proven

Each item cites its evidence. Code references are to `main` at bc2bf43 and to
franklinwh-modbus **v0.9.5**, the version deployed.

### 2.1 Dispatch path

| # | Fact | Evidence |
|---|---|---|
| P1 | A send is three separate write steps: **stop** (WSetEna=0), **configure** (WSetMod=0, WSetPct), **enable** (WSetEna=1), with 0.3–0.5 s sleeps. If it fails after step 1, the battery is left **stopped**. | library `controller.py` `send_command._do_send` (v0.9.5) |
| P2 | The library's "VERIFY" step reads the registers back but **does not check them**. It returns `True` whatever WSetEna reads. | same; step 4 only logs |
| P3 | A failed send is retried once, after a reconnect (`_with_retry`, max 2 attempts). The reconnect itself can fail. | library `_with_retry` |
| P4 | The bridge sets the command **active** (action, power, started_at) **whether or not the send succeeded**. | `publish/command_handler.py`, the send path after `send_command` |
| P5 | The scheduler audits **"ok — dispatched"** and records ownership of the window **unconditionally**. While it owns the window it does not re-dispatch. | `gateway/scheduler.py` dispatch → `_audit(..., "ok", "dispatched …")`, `self._owned[tkey]` |
| P6 | Catch-up runs only after a declared connectivity **outage** (no successful poll for more than 60 s). A single failed write isn't one. | `scheduler.catchup()` |
| P7 | Correct registers don't guarantee the battery moves: #26 had WSetEna 1 and WSetPct 100 with `battery_w` 0 for 38 min. A re-send then worked at once. | #26 (control_log, metrics) |
| P8 | Every 18:00 dispatch from 09-29 to 10-11 recorded WSetEna 1. 10-12 is the only one with WSetEna 0. | `control_log` |
| P9 | franklinwh-modbus `develop` (2774bb2, unreleased) adds `verify_dispatch()`: it reads back WSetEna/WSetPct **and** 714.DCW, with a tolerance and a ramp timeout. Its own commit says "hardware verification deferred". | library git |

### 2.2 The Modbus connection (snapshots and logs, 2026-10-12)

| # | Fact | Evidence |
|---|---|---|
| C1 | The bridge holds **two** Modbus connections to the aGate: the library's sunspec2 client, and a separate pymodbus client for vendor registers (15000–15039, 15510–15513, 16000–16002). It also opens a TCP probe **every 60 s** (5 s timeout). | `modbus/poller.py` `_get_vreg_client`; `gateway/health.py` |
| C2 | The vendor-register client is **closed on any exception** and re-created on the next poll (every 10 s). | `poller._read_vendor_registers` → `_close_vreg_client()` |
| C3 | Every library reconnect discards the client, sleeps 1 s and **rescans all models** (17 on this aGate) before the operation is retried. | library `reconnect()` |
| C4 | `Connection issue (Response timeout) … reconnecting` appears **1,249–2,289 times a day** (10-09 to 10-12), rising to 130–170/h on 10-12. | container logs |
| C5 | In the hour to about 19:40 on 10-12: **18** `Reconnection failed`. | container logs |
| C6 | In a 60 s sample: the bridge had **at least 11** distinct connections to 192.168.0.110:502, with at most 2 open at once. The Local Bridge (`franklinwh-direct-connect-bridge`) had **at least 9**, also at most 2. Sampling every 2 s misses short ones. | `/proc/net/tcp` in each container's network namespace |
| C7 | The Local Bridge is a Modbus client of this aGate: it has ESTABLISHED and TIME_WAIT sockets to :502, and its log shows repeated `franklinwh_modbus.controller: Connection failed: Response timeout`. | the same, plus its container log |
| C8 | When reads fail, values from the cached model are still published as current ("Derived 4 battery key(s) from M713 model cache"). | #25; `poller` |

## 3. What is not known

Each unknown has the experiment that answers it (§6.1).

| # | Unknown | Why it matters | Experiment |
|---|---|---|---|
| U1 | How many concurrent Modbus TCP connections the aGate accepts, and what happens to an existing one when another opens (refused? the older one dropped? both slowed?) | Decides whether the connection design (§5.2) must be single-connection | E1, E2 |
| U2 | The aGate's response-time distribution with one quiet client | Sets timeouts, and separates "slow device" from "contention" | E3 |
| U3 | The cost of a full model scan, and whether frequent reconnects or scans themselves cause timeouts | Decides whether to reconnect without rescanning | E4 |
| U4 | Whether the 10-12 timeout was caused by contention, churn or the device | Root cause of (B) | E1–E5 together |
| U5 | Whether re-sending the 3-step sequence during an active discharge interrupts output (P1) | Decides the retry and re-assert write strategy | E6 |
| U6 | How long after a correct setpoint the battery reaches the commanded power (ramp) | Sets the verification delay and tolerance | E7 |
| U7 | Why the aGate ignored a correct setpoint in #26 (mode TOU at the time?) | Whether re-send is the right remedy, or a mode change is needed first | E7, plus a review of #26 |
| U8 | Whether the Energipays containers touch the aGate (TIME_WAIT entries seen, but no Modbus in their logs) | Contention inventory | E0 |

## 4. Goals

1. **No silent failure.** Every dispatch ends in a recorded, truthful state:
   `verified`, `failed` or `not_effective`. The owner is notified on anything
   but `verified`.
2. **Recover within the window.** A transient failure is retried and recovered
   automatically while the window is open.
3. **One healthy link.** The bridge's own use of the aGate is deliberate and
   minimal. Timeouts drop to a level the measurements say is normal.
4. **Stale data is never shown as live.**

**Non-goals:**
- changing schedule semantics (Pause/Resume is #32);
- controlling other projects' clients (an owner decision, §9).

## 5. Design

### 5.1 (A) Dispatch with verification. Independent of the cause; ships first.

**States per dispatch:**

```
requested → sent ──(read-back ok and battery moving)──→ verified
               │                                         │ (each tick while the window is open)
               │                                         └─(lost)─→ reassert → verified | failed
               ├─(send error)──────────────→ failed ──(retry within window)──→ sent …
               └─(registers ok, battery not moving by T_verify)──→ not_effective ──(one re-send)──→ …
```

1. **A failed send is a failure.**
   - The command handler sets `active` only when `send_command` succeeds **and**
     a register read-back shows WSetEna=1 with WSetPct matching (fixes P2, P4).
   - Otherwise it audits `failed` with the reason, logs a WARNING, and leaves
     the window **unowned** so the scheduler retries (fixes P5).
2. **Retry inside the window.**
   - Retry on the next scheduler tick (15 s), with backoff of 15 s, 30 s, 60 s,
     then every 60 s.
   - Stop at window end.
   - Each attempt is audited.
3. **Verify the effect** (P7).
   - From `T_verify` after a send, check 714.DCW. `T_verify` is set from E7
     (provisionally 90 s).
   - The direction must match and the magnitude must be within tolerance,
     capped by SoC and rating limits.
   - If not: audit `not_effective` and re-send **once**. If still not
     effective: audit `failed` and notify.
   - Uses `verify_dispatch()` semantics (P9): the library's once it is
     released and hardware-verified, otherwise the same rule in the bridge.
4. **Supervise while the window is open.**
   - Each tick, read WSetEna/WSetPct, which the poller already reads.
   - If the command was lost (WSetEna=0, or WSetPct wrong), **re-assert** and
     audit `reasserted`.
   - Re-assert writes only the registers that differ. It must not run the stop
     step while output is correct (P1); the write strategy depends on E6.
5. **Release is verified too:** WSetEna=0 is read back after release.
6. **Notify** through the entry's HA notify target on `failed`, on
   `not_effective`, and on recovery. Example: "18:00 Battery Bonus TOU Export:
   FAILED (Response timeout), retrying".
7. **Show the truth** in the UI: commanded vs actual on Schedule and Controls,
   and a red marker for any failed or not-effective dispatch.

### 5.2 (B) The Modbus connection. After the measurements; shape depends on U1–U4.

**Direction** (to be confirmed by E1–E4):

- **One connection per aGate, one owner.** A single session object owns the TCP
  connection, and every read, write and health check goes through its queue
  (the existing `modbus_lock`, extended to all users).
  - The vendor-register reads move onto the same connection (C1, C2). That may
    need a library hook to read raw registers through the sunspec2 client.
  - Health comes from the session's own success and failure history, not a
    separate TCP probe (C1).
- **Not a pool.** More connections is the wrong direction for a device whose
  connection limits are unknown (U1).
- **Cheap reconnect.** Reconnect re-opens the socket and reuses the scanned
  model layout. A full rescan only on start-up, or when the layout check fails
  (C3, U3). This is a library change; it needs coordinating with fwh-modbus.
- **Backoff** on repeated failure instead of an immediate reconnect and rescan
  storm.
- **Staleness.** Values from a failed read are marked stale. Sensors carry the
  quality and age, not just the cached value (C8, #25).
- **Observability.** Per-gateway counters: connections opened, timeouts,
  reconnects, failed reconnects and scan time. Shown in diagnostics, so (B) can
  be measured before and after the change.

### 5.3 (C) Other clients

The bridge cannot control them. It can **detect and report** them: the
diagnostics page can show other recent connections where they are visible, and
link timeout spikes to them. Whether the Local Bridge, or anything else, should
use Modbus on the reference aGate is an owner decision (§9).

## 6. Proof: test rig and plan

Nothing in §5 counts as done until it passes these, in order.

### 6.1 aGate characterisation (live, coordinated window)

**Preconditions:**
- the owner schedules it outside 17:30–20:00;
- this bridge's poller is stopped through the API;
- the Local Bridge's Modbus is off (owner);
- E0 confirms no other client;
- every write experiment has an operator watching and a release ready.

**Tool:** a measurement script, `tools/agate_probe.py`, that logs every
request's timestamp, latency and result to CSV.

| Exp | Procedure | Output |
|---|---|---|
| E0 | Inventory: list connections to :502 from every container and host for 10 min | Who connects, and how often |
| E1 | Open 1, 2, 3 … N concurrent connections; each reads M1 every 2 s for 2 min | Max accepted; latency and failures per N |
| E2 | Connection A polls steadily; B opens, reads and closes every 5 s | Does A time out, get dropped or slow down when B connects? |
| E3 | One client, 10 s poll for 30 min | Latency p50/p95/p99; timeout rate at rest |
| E4 | Timed full scans; then 30 reconnect+scan cycles | Scan duration; whether rapid rescans induce timeouts |
| E5 | Replay the bridge's current pattern (2 connections, close-on-error and a probe) against E3's baseline | Whether today's pattern alone reproduces the timeout rate |
| E6 | **Write.** At 1 kW discharge, re-send (a) the full 3-step sequence and (b) changed registers only. DCW sampled every 1 s. | Whether a re-send dips output |
| E7 | **Write.** Send discharge at 1 / 3 / 5 kW in Self-Consumption and in TOU; DCW every 1 s | Ramp time; TOU behaviour (U7) |

**Result:** `docs/results/2026-10-xx_agate-characterisation.md`, with the CSVs.
Every U-item gets an answer, or is explicitly left open.

### 6.2 Emulator fault injection (automated, before any hardware)

The emulator (`franklinwh-gateway-emulator`) currently has no way to inject
faults. Requirements, to be shaped by §6.1:

- Per-request faults: response timeout, connection reset, and delays of N ms.
- Connection limits: at most N clients, with the behaviour E1/E2 found.
- **Ignored setpoint:** accept WSetEna/WSetPct but keep DCW at 0 (#26).
- **Lost setpoint:** WSetEna drops to 0 mid-window.
- Scriptable from tests (pytest fixture), so each scenario is deterministic.

This is another repository; the owner decides who builds it.

### 6.3 Automated acceptance tests (bridge, against the emulator)

| Test | Scenario | Must show |
|---|---|---|
| T1 | 10-12 replay: the first send times out | `failed` audited, not `ok`; not active; retry at the next tick; `verified` when the emulator answers; notification sent |
| T2 | 10-07 replay: setpoint accepted, DCW 0 | `not_effective` at T_verify; one re-send; `verified` or `failed` plus notification |
| T3 | 10-04 replay: socket dead for the window | Reads stale (not live); dispatch `failed` and notified; recovered on reconnect within the window |
| T4 | WSetEna drops mid-window | `reasserted` within one tick; output restored |
| T5 | Re-assert while output is correct | No write if the registers already match; no dip (E6) |
| T6 | Every attempt fails until window end | Bounded retries, stop at the end, one clear `failed` summary, no hammering |
| T7 | Release | WSetEna=0 read back; `released` only when verified |

### 6.4 Live acceptance (coordinated)

- **One controlled run:** the real 18:00 entry, plus one induced failure,
  stopping the poller mid-send if E1–E6 show that is safe.
- Then **14 consecutive evenings** where every dispatch is `verified`,
  `failed`-and-notified, or recovered. Zero silent misses.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | No dispatch is audited `ok` or `verified` unless the read-back **and** DCW confirm it |
| A2 | A failed send is retried within 30 s, and the owner is notified within 60 s |
| A3 | Not-effective is detected within T_verify + 15 s |
| A4 | A lost command in an open window is re-asserted within one tick (15 s) |
| A5 | Stale values are never published as live (#25) |
| A6 | Phase 2: the timeout rate falls to within the E3 baseline (number set from E3) |
| A7 | T1–T7 pass in CI against the emulator; §6.4 passes live |

## 8. Phasing

1. **Phase 0 (this document):** review, decisions (§9), E0–E7, and emulator
   fault injection.
2. **Phase 1, dispatch verification (§5.1):** fixes #26 and #56, and the alert
   half of #25. It doesn't wait for (B).
3. **Phase 2, connection (§5.2):** fixes the cause side of #25, and (B) once
   E1–E5 say what is right. It includes the library changes (cheap reconnect,
   raw-register hook) and a franklinwh-modbus release.
4. **Phase 3:** UI for commanded vs actual, and diagnostics counters (A6).

**Interim, until Phase 1 ships:** nothing in the bridge protects a dispatch. The
owner can watch the 18:00 notification and the battery power, and re-send by
hand.

## 9. Owner decisions

1. **Approve this design**, or change it.
2. **A window for E0–E7**, including permission for the write experiments
   E6 and E7.
3. **Other clients:** should the Local Bridge (or anything else) use Modbus on
   the reference aGate? If it should, E1/E2 decide whether sharing is safe.
4. **Emulator fault injection:** who builds it (the emulator repository).
5. **Library changes** (cheap reconnect, raw-register access, releasing
   `verify_dispatch`): coordinate with fwh-modbus.
6. **Notification wording and target** for failed dispatches.

## 10. Risks

- **Re-sending can disrupt output** (P1). Mitigation: E6 decides the write
  strategy; only differing registers are written.
- **Verifying by DCW** can be fooled by SoC or rating limits, or by a slow ramp.
  Mitigation: tolerance and timing from E7; account for the reserve and the
  limits.
- **Retries add traffic to a strained link.** Mitigation: bounded backoff;
  Phase 2 reduces baseline traffic.
- **The cause of (B) may stay unknown.** Phase 1 still prevents silent loss,
  which is why it goes first.
