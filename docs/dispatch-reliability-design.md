# Dispatch reliability and the Modbus connection: design

**Status:** DRAFT for owner review (2026-10-12). Library facts fact-checked by fwh-modbus 2026-10-12: P1–P3 and C3 confirmed with additions, P9 updated, C1 corrected (per-call TCP, verified in the deployed pysunspec2 1.3.6). No code until this is
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
| P1 | A send is three separate write steps: **stop** (WSetEna=0), **configure** (WSetMod=0, WSetPct), **enable** (WSetEna=1), with 0.3–0.5 s sleeps, then a read-back. Each `write()` writes only the points changed since the last one. A retry re-runs from step 1, so it **repeats WSetEna=0**. If it fails after step 1, the battery is left **stopped**. | library `controller.py` `send_command._do_send` (v0.9.5); fwh-modbus fact-check 2026-10-12 |
| P2 | The library's "VERIFY" step reads the registers back but **does not check them**. It returns `True` whatever WSetEna reads. | same; step 4 only logs |
| P3 | A failed send is retried once, after a reconnect (`_with_retry`, max 2 attempts). The reconnect itself can fail, and then `send_command` returns `(False, msg)`. **Any** exception whose text contains "socket", "timeout", "connection" or **"modbus"** counts as a connection failure. That includes the device's own Modbus *exception replies*, so those also trigger a reconnect and full rescan. | library `_with_retry`; fwh-modbus fact-check |
| P4 | The bridge sets the command **active** (action, power, started_at) **whether or not the send succeeded**. | `publish/command_handler.py`, the send path after `send_command` |
| P5 | The scheduler audits **"ok — dispatched"** and records ownership of the window **unconditionally**. While it owns the window it does not re-dispatch. | `gateway/scheduler.py` dispatch → `_audit(..., "ok", "dispatched …")`, `self._owned[tkey]` |
| P6 | Catch-up runs only after a declared connectivity **outage** (no successful poll for more than 60 s). A single failed write isn't one. | `scheduler.catchup()` |
| P7 | Correct registers don't guarantee the battery moves: #26 had WSetEna 1 and WSetPct 100 with `battery_w` 0 for 38 min. A re-send then worked at once. | #26 (control_log, metrics) |
| P8 | Every 18:00 dispatch from 09-29 to 10-11 recorded WSetEna 1. 10-12 is the only one with WSetEna 0. | `control_log` |
| P9 | franklinwh-modbus `develop` adds `verify_dispatch()` (2774bb2): it reads back WSetEna/WSetPct **and** 714.DCW, with a tolerance and a ramp timeout. **Hardware-tested** on the reference aGate, 2026-10-09 16:05–16:10:<br>• −1500 W discharge: full output from +2.1 s, verified on 86 of 87 samples.<br>• +1500 W charge at 99% SoC: the device gave 0 W, and verify_dispatch reported "battery not responding" on every sample.<br>• Every release read back as 0.<br>Still **unreleased** (develop only; 0.9.5 lacks it). | library git; `tests/results/2026-10-09_pr23_live.md`; fwh-modbus fact-check |

### 2.2 The Modbus connection (snapshots and logs, 2026-10-12)

| # | Fact | Evidence |
|---|---|---|
| C1 | **The library's connection is not persistent.** After `scan()`, pysunspec2 holds no socket: each `read()`/`write()` opens a TCP connection and closes it when done (`ModbusClientTCP.read`: `local_connect` → `disconnect()`). One `send_command` is therefore about 7 TCP opens, and every 10 s poll opens one per model read. **The only long-lived connection** is the bridge's own pymodbus client for vendor registers (15000–15039, 15510–15513, 16000–16002). The bridge also opens a TCP probe **every 60 s** (5 s timeout). | pysunspec2 1.3.6 `modbus.py` (deployed, verified); `modbus/poller.py` `_get_vreg_client`; `gateway/health.py`; fwh-modbus fact-check |
| C2 | The vendor-register client is **closed on any exception** and re-created on the next poll (every 10 s). | `poller._read_vendor_registers` → `_close_vreg_client()` |
| C3 | Every library reconnect discards the client, sleeps 1 s and **rescans all models** (17 on this aGate) before the operation is retried. It calls `scan()` **without** the base address `connect()` uses, and the scan closes its socket at the end. | library `reconnect()`; fwh-modbus fact-check |
| C4 | `Connection issue (Response timeout) … reconnecting` appears **1,249–2,289 times a day** (10-09 to 10-12), rising to 130–170/h on 10-12. | container logs |
| C5 | In the hour to about 19:40 on 10-12: **18** `Reconnection failed`. | container logs |
| C6 | In a 60 s sample: the bridge had **at least 11** distinct connections to 192.168.0.110:502, with at most 2 open at once. This is consistent with C1's per-call connections, not with reconnects alone. The Local Bridge (`franklinwh-direct-connect-bridge`) had **at least 9**, also at most 2. Sampling every 2 s misses short ones. | `/proc/net/tcp` in each container's network namespace |
| C7 | The Local Bridge is a Modbus client of this aGate: it has ESTABLISHED and TIME_WAIT sockets to :502, and its log shows repeated `franklinwh_modbus.controller: Connection failed: Response timeout`. | the same, plus its container log |
| C9 | **The library is synchronous; the bridge is async.** Each library call runs in a worker thread (`asyncio.to_thread`). pysunspec2 and controller objects are not safe to use from two threads, so correctness depends on **every** call going through the per-gateway `modbus_lock`. | `poller.py`, `command_handler.py`, `instance.py` |
| C10 | **Two sockets at once during every poll.** Inside the lock, the vendor-register pymodbus client stays connected while the library opens its own per-call connections (C1). Serialised, but on **two concurrent TCP connections**. This matches C6's "at most 2 open at once". | `poller._poll_once` → `_read_extra_points`/`_read_vendor_registers` |
| C11 | **CONFIRMED by fwh-modbus (v0.9.5 and develop).** **The library's own timer thread writes outside the lock.** When a command carries a duration (`duration_s` > 0; e.g. 2026-10-08 05:30 "[timeout: 2400s]"), `send_command` starts a `threading.Timer` whose expiry calls `reset_control_state()` (a 704 write) from that thread. That is outside the bridge's `modbus_lock`, so it can run during a poll or a send. | library `send_command` step 5 (v0.9.5); bridge passes `duration_s` when the watchdog is > 0 |
| C14 | **The library has no I/O lock.** The only lock (`_command_timer_lock`) guards the timer object. No lock covers Modbus I/O, `self.dev`, `self.models` or the point objects, and pysunspec2 has none either. If two threads share a controller (code reading; not observed):<br>• one shared model object per model means a `read()` can overwrite values another thread set before its `write()`, and `write()` sends every dirty point;<br>• a shared socket means one thread's `finally: disconnect()` can close the other's connection;<br>• **pysunspec2 sends transaction id 0 and never checks the reply's id**, so interleaved requests can take each other's responses and return **wrong values with no error**. | fwh-modbus fact-check (library v0.9.5/develop, pysunspec2 1.3.3 `modbus.py` _read) |
| C15 | **`reconnect()` is not thread-safe:**<br>• it closes the device under any in-flight read;<br>• for 1 s `self.dev` is None while `self.models` still holds old models, and `'NoneType' … 'client'` matches none of `_with_retry`'s keywords, so there is no retry;<br>• two threads can rescan at once.<br>The timer's `reset_control_state()` (C11) runs inside `_with_retry`, so it can also trigger `reconnect()` from the timer thread. `Timer.cancel()` can't stop a callback that has already started. | fwh-modbus fact-check |
| C16 | **"Response timeout" means the connection was closed, not a slow reply.** In pysunspec2 1.3.6 (deployed), TCP `_read` raises `ModbusClientTimeout('Response timeout')` when `socket.recv()` returns 0 bytes, i.e. the connection was **closed** before the full reply. A genuine socket timeout raises "timed out" instead. So C4's thousands a day, and the 10-12 18:00 failure, are **connections closed mid-request**: most likely by the aGate; a local shutdown from another thread isn't ruled out. That fits a connection limit or second-client eviction (U1/U2) but doesn't prove it. | pysunspec2 1.3.6 `modbus.py` (verified in the container); fwh-modbus |
| C12 | **Start-up calls run without the lock:** connect, nameplate, the hardware-state snapshot and the stale-command release (`reset_control_state`). They run **before** `poller.start()`, so they cannot overlap polling. Whether a command dispatched during start-up can overlap them is **not checked**. | `instance._init_and_poll` |
| C13 | **The diagnostics probe can abandon a call while it is still running.** `asyncio.wait_for(to_thread(...))` under the lock: on timeout the lock is released, but the thread keeps reading, so the next lock holder overlaps it. Only when the diagnostics probe is used. | `poller.probe_protocol` |
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
| U6 | How long after a correct setpoint the battery reaches the commanded power (ramp). **Partly answered:** a −1500 W discharge reached full output within 2.1 s (the sampling interval) on 2026-10-09. Charge, other powers and TOU mode are not measured. | Sets the verification delay and tolerance | E7 (remaining cases) |
| U7 | Why the aGate ignored a correct setpoint in #26 (mode TOU at the time?). One related data point: on 2026-10-09 a charge at 99% SoC was accepted with 0 W delivered. That doesn't establish #26's cause (a discharge, in TOU). | Whether re-send is the right remedy, or a mode change is needed first | E7, plus a review of #26 |
| U10 | Whether overlapping requests (C10, C11, C13, or another client) happen in practice, and whether they cause timeouts or garbled reads. The 2026-07-05 reading had the same 16-bit word in two unrelated registers (#55), which is what overlapping requests could produce; one event, not established. | Decides how strict the single-owner rule must be | Phase 0b overlap counter; E1/E2 |
| U9 | Whether Modbus *exception replies* that trigger reconnect and rescan (P3) account for part of C4's reconnect rate. **Hypothesis**, from fwh-modbus. | If so, a cheap fix: don't treat device exception replies as dead links | Classify logged exceptions (Phase 0b metrics) |
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
5. **Provide for the Modbus Bridge becoming the single Modbus gateway** for
   each aGate (owner, 2026-10-12). Sibling bridges would use its API instead
   of opening :502. **A future branch, not built in this design.** The current
   phases must not preclude it (§5.5).

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

- **One connection per aGate, one owner, actually kept open.** Today nothing is
  persistent (C1): pysunspec2 connects per call. Keeping one socket open means
  connecting `dev.client` once and keeping it (the library already reaches it
  as `self.dev.client` for its extension-register paths, and the client has
  `read(addr, count)`). This is a library change. A single session object owns
  the TCP connection, and every read, write and health check goes through its queue
  (the existing `modbus_lock`, extended to all users).
  - The vendor-register reads move onto the same connection (C1, C2). That may
    need a library hook to read raw registers through the sunspec2 client.
  - Health comes from the session's own success and failure history, not a
    separate TCP probe (C1).
- **Everything through the one queue:** polls, vendor registers, commands,
  start-up calls and health (C9–C13).
  - No library-side timer thread writes on its own (C11). The bridge stops
    passing `duration_s` to the library and relies on its own command watchdog,
    which already exists.
  - A timeout on a queued call must not release the queue while its thread is
    still running (C13).
- **Multi-gateway: one connection per aGate, managed per gateway.** A
  per-gateway connection manager, keyed by gateway in the existing registry,
  gives each aGate exactly one owned connection. It never pools several
  connections to the *same* aGate. That is the wrong direction for a device
  whose connection limits are unknown (U1).
- **Precedent, verified in source:** Home Assistant's own Modbus integration,
  `homeassistant/components/modbus/modbus.py` `ModbusHub` (HA 2026.3.1, read
  in the `fwhhai-homeassistant` container), does the same:
  - one pymodbus client per hub, opened once;
  - every call serialised through one `asyncio.Lock`;
  - a failed call is logged and returns `None` without tearing the connection
    down;
  - connect retries after a fixed delay;
  - no model rescan.

  Today the bridge does the opposite on each point (C1–C3).
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

### 5.4 Measurement (Phase 0b, ships before any fix)

**Why first:**
- **A baseline.** A6 ("timeouts fall to the baseline") needs today's rates
  recorded *before* anything changes; once a fix ships, the "before" is gone.
- **The tools today aren't good enough.** On 2026-10-12 the picture had to be
  rebuilt by hand from logs and socket sampling, and parts of it were wrong:
  - library lines carry no timestamp;
  - Docker timestamps are UTC;
  - a recreated container loses its history;
  - sampling every 2 s misses short connections.
- **No risk.** It is read-only instrumentation; it never touches dispatch.

**Always on**, per gateway, rolled up per hour, kept 90 days, in a new
`link_stats` table:

| Area | Metrics |
|---|---|
| Requests | Count, ok, timeout and error, per kind: model read, vendor-register read, write. Exceptions classified by type and text, which answers U9. In particular "Response timeout" (connection closed, C16) is counted separately from "timed out" (no reply). |
| Latency | p50, p95 and max per hour, per kind |
| Connections | Opened per path (library per-call, vendor client, health probe); reconnects; failed reconnects; rescans and rescan duration |
| Freshness | Consecutive failures; age of the newest good reading; polls served from the cache instead of live (C8) |
| Overlap | Requests that **start while another is still in flight** to the same aGate, with which code path sent each one (poller, vendor client, command, start-up, diagnostics, library timer). Also the maximum number of open sockets per poll. This answers U10. |

Plus a **`dispatch_outcomes` table**, one row per dispatch:
- entry id;
- requested and sent times;
- final state (`verified` / `failed` / `not_effective` / `reasserted`);
- time to verify, retries and re-asserts;
- commanded vs actual power, and later the energy exported in the window.

A daily summary must show **zero silent misses**.

**How it is collected without waiting for the library:**
- the bridge times and wraps each library call it makes;
- a logging handler on the `franklinwh_modbus` logger counts library-internal
  events ("Connection issue … reconnecting", "Reconnection failed");
- a `pysunspec2` connect counter, if the per-call count is needed exactly.

Exact rescan timing comes later from library hooks (§9.5).

**Optional, off by default:** a per-request trace (time, kind, latency and
result for every request), switched on for experiments E0–E7 and then off.
It is too noisy to keep.

**Exposed:**
- REST `GET /api/gateways/{id}/link-stats` and `GET /api/dispatch-outcomes`,
  REST first;
- a "Link health" panel on the gateway diagnostics page;
- optional HA diagnostic sensors: timeouts per hour, minutes since the last good
  poll, and the last dispatch's outcome.

### 5.5 Gateway API for sibling bridges (FUTURE BRANCH: designed for, not scheduled)

**Status (owner, 2026-10-12):** a future branch, not critical now. This design
must **provide for it**: nothing in Phases 0b–3 may make it harder.
§5.5.1 lists the provisions the current phases must include. The rest of §5.5
is the target API, kept here so those provisions point the right way.

**Goal of the branch:** this bridge owns the only Modbus connection to each
aGate, and other bridges read and control the aGate through it.

#### 5.5.1 Provisions in the current phases (in scope)

| Provision | Where | Why the gateway needs it |
|---|---|---|
| All aGate I/O through one per-gateway queue, including the library timer and start-up calls | §5.2 (Phase 2) | The gateway *is* the single owner; a consumer request becomes one more queued job |
| Every dispatch has a **`dispatch_id`**, a **`requester`** (schedule / UI / HA / API) and an optional **`client_ref`**, and moves through the §5.1 states, persisted in `dispatch_outcomes` | §5.1, §5.4 (Phases 0b and 1) | The atomic dispatch and status endpoints are thin wrappers over this, and arbitration needs a requester |
| The command path takes **one request object** `{action, power, duration, target_soc}`, not staged separate settings | §5.1 (Phase 1) | Atomic dispatch; removes the interleaving staging pattern |
| Every telemetry value is served with **`ts`, `age_s`, `quality` / `stale`** from the poll cache | §5.2, §5.4 (Phases 0b and 2) | Cache-only fan-out to consumers with honest freshness |
| An explicit **gateway id** on every internal dispatch and read | Phase 1 | Consumers on multi-gateway sites must address a specific aGate |
| The audit records **who requested** each dispatch | Phase 1 (#50 audit, `dispatch_outcomes`) | Consumer accountability |
| Link-health counters per gateway | §5.4 (Phase 0b) | Consumers can judge whether data is fresh |

None of these adds scope beyond what Phases 0b–2 already need. They fix the
*shape* of it.

#### 5.5.2 Target API (future branch)

##### Consumers today (evidence, read-only, 2026-10-12)

| Consumer | What it does now | Problems |
|---|---|---|
| **Local Bridge** (`franklinwh-local-bridge`) | **Dispatch through us:** `providers._dispatch_via_modbus_bridge` makes several `POST /api/command {slug, value}` calls (stage power, duration and target SoC, then `battery_command`), when `MODBUS_BRIDGE_URL` is set. That is empty on the reference site. **Billing:** logs in, then `GET /api/tariff/history`. **Direct Modbus:** `battery_control.py` creates a new `FranklinWHController` per status read or command, i.e. connect plus full scan each time (C3). | **Not atomic:** staging calls from two clients can interleave. **No login:** `/api/command` requires a session with the control capability, so these calls would get 401 today. **No gateway id:** always the default gateway, on multi-gateway sites too (its own BR-34 note). **Its direct path is a second Modbus client** (C6, C7). |
| **Energipays Bridge** | The owner says it will use the gateway. Its needs are **not yet known**: its logs show cloud and HA polling, not Modbus. | To be gathered |

##### API, version 1

REST first, as with everything else here.

1. **Service accounts and API tokens.**
   - Each consumer gets a named account with a bearer token, scoped by
     capability (`view`; `control` for dispatch). No cookie login.
   - Every call is recorded in the audit trail (#50 `auth_events`) under the
     consumer's name.
   - Tokens can be revoked and rotated.
2. **Discovery:** `GET /api/gateways` (exists). Returns ids, serials and names,
   so a consumer always addresses an explicit gateway.
3. **Telemetry fan-out, with no extra Modbus reads.**
   - `GET /api/gateways/{id}/points` (exists), extended with `ts`, `age_s`,
     `quality` and `stale` per response (C8).
   - Served **only from the poll cache**: a consumer can never trigger an aGate
     read.
   - A documented **consumer schema**: the stable keys, units and sign
     conventions consumers may rely on. Everything else is unsupported.
   - Raw vendor registers (`vreg_*`) are also cache-only.
4. **Atomic dispatch.**
   - `POST /api/gateways/{id}/dispatch` takes
     `{action, power_w | power_pct, duration_s, target_soc, client_ref}` in
     **one call** and returns a `dispatch_id`.
   - `GET …/dispatch/{dispatch_id}` returns the §5.1 state: `sent` /
     `verified` / `failed` / `not_effective` / `reasserted` / `released`.
   - `POST …/dispatch/{dispatch_id}/release`.
   - `client_ref` is an idempotency key, so a consumer can retry safely without
     a double dispatch.
5. **Arbitration.** One battery, several requesters: the schedule, the UI,
   HA, and consumers.
   - A defined priority. **Owner decision; a proposal for discussion:**
     safety release > manual (UI/HA) > schedule > consumer.
   - A conflicting request gets `409` naming the current owner of the battery,
     or queues if the policy says so.
   - The bridge's watchdog and orphan release cover consumer dispatches too.
     The Local Bridge already delegates to us for exactly that reason (its
     `providers.py`).
6. **Events.**
   - Dispatch state changes and link-health changes are pushed: on MQTT, which
     the bridge already runs, and/or Server-Sent Events. Consumers needn't
     poll.
   - The transport is an owner decision.
7. **Link health:** `GET /api/gateways/{id}/link-stats` (§5.4), so a consumer
   knows whether the data is fresh and the link is healthy.
8. **Contract and versioning.**
   - Published OpenAPI with the consumer schema.
   - Breaking changes only with a new version and a deprecation period.
   - `POST /api/command` stays for the UI and HA, but is **deprecated for
     consumers**.
9. **What it cannot enforce.** The bridge cannot stop another process
   connecting to :502. What enforces the rule is:
   - consumers removing their direct path (e.g. the Local Bridge's
     `battery_control.py` library path; its owner's change);
   - detection here: E0 and the §5.3 diagnostics show any other client.

##### Proof (for the branch)

| Test | Shows |
|---|---|
| G1, contract | Every documented endpoint and field, against the OpenAPI schema |
| G2, atomicity | Two consumers dispatching at once: each gets one consistent result; no interleaved staging |
| G3, idempotency | The same `client_ref` twice gives one dispatch |
| G4, arbitration | Schedule vs consumer vs manual follow the priority; the loser gets `409` with the owner named |
| G5, fan-out | Many consumer reads produce **zero** extra aGate requests (§5.4 counters) |
| G6, auth and audit | No token: 401. View token on dispatch: 403. Every call audited with the consumer's name. |
| G7, live | With the Local Bridge using the API and its direct path removed, E0 shows **one** client on :502 |

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
| A6 | Phase 2: the timeout rate (from `link_stats`) falls to within the E3 baseline. The number is set from E3 and the Phase 0b baseline. |
| A9 | Provision: every Phase 1 dispatch row has `dispatch_id`, `requester` and a gateway id; telemetry carries `ts`/`age_s`/`quality`. (G1–G7 belong to the future branch.) |
| A8 | Phase 0b: at least 7 days of `link_stats` baseline recorded before Phase 1 or 2 ships, and `dispatch_outcomes` recording every dispatch |
| A7 | T1–T7 pass in CI against the emulator; §6.4 passes live |

## 8. Phasing

1. **Phase 0 (this document):** review, decisions (§9), E0–E7, and emulator
   fault injection.
2. **Phase 0b, measurement (§5.4):** ships first, then collect at least a 7-day
   baseline. It runs alongside Phase 0.
3. **Phase 1, dispatch verification (§5.1):** fixes #26 and #56, and the alert
   half of #25. It doesn't wait for (B).
4. **Phase 2, connection (§5.2):** fixes the cause side of #25, and (B) once
   E1–E5 say what is right. It includes the library changes (cheap reconnect,
   raw-register hook) and a franklinwh-modbus release.
5. **Phase 3:** UI for commanded vs actual.
6. **Future branch (not scheduled): gateway API (§5.5.2).** Tokens, atomic
   dispatch, arbitration and events, on top of the §5.5.1 provisions. Then the
   consumers switch in their own repos, and E0/G7 confirm a single client.

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
7. **Gateway API (§5.5, future branch):** when to start it; the arbitration priority; the event transport (MQTT
   and/or SSE); the token model; Energipays' needs; and when the Local Bridge
   removes its direct Modbus path.

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

## 11. Future recommendation: modbus-connection as the transport (OUT OF CURRENT SCOPE)

> **Not part of this design's scope or phasing.** This section records an
> evaluation requested by the owner on 2026-10-12, so the reasoning is
> available when the decision comes up. Nothing in Phases 0–3 depends on it,
> and nothing here is scheduled.

### 11.1 What it is

[modbus-connection](https://github.com/home-assistant-libs/modbus-connection),
from the Open Home Foundation (home-assistant-libs):
- Apache-2.0; async only; Python ≥ 3.12.
- Backends: pymodbus ≥ 3.11 or tmodbus.
- Evaluated by balloob against about 60 Python Modbus libraries
  ([gist](https://gist.github.com/balloob/b9fa91ba1a0914a9787f8f6ceb637b83),
  2026-07-02). That gist rates franklinwh-modbus "Friction": it auto-reconnects,
  sleeps between writes, and has a watchdog and read-back verify. It treats the
  watchdog and verify as the owner's job, not gaps.
- **Checked 2026-10-12 (PyPI):**
  - latest 4.12.4;
  - 41 releases since 1.0.0 on 2026-06-24;
  - major versions 1–4 in about 3.5 months.
- **Bridge runtime:** Python 3.12.15 and pymodbus 3.13.1 in the container, so
  the requirements are met. `pyproject.toml` declares ≥ 3.11 and would need
  raising.
- **Features, per its README and docs** (summarised by fwh-modbus; not
  independently tested here):
  - one owner-held connection; requests serialised so frames never interleave;
  - `for_unit()` handles that survive reconnects;
  - connects on first use, reconnects on the next request without rescanning;
  - `disconnect()` recycles a wedged link, waiting at most 0.5 s for the
    request in flight;
  - `message_spacing` paces requests;
  - typed exceptions;
  - SunSpec `scan()`, `SunSpecComponent` with a model-header check
    (`SunSpecMapShiftError`), components generated from the official model
    JSON (0-based V1.2 enums), and scale factors handled on reads and writes.

### 11.2 Comparison against our findings

| Finding (§2) | Today: franklinwh-modbus v0.9.5 + pysunspec2 | modbus-connection |
|---|---|---|
| C1: a TCP connection per read/write | Opens and closes per call (about 7 per send) | One persistent connection |
| C3: full rescan on every reconnect | Yes (all 17 models; `scan()` without base address) | Reconnects without rescan |
| C9/C14: no I/O lock; shared model and socket objects | Correctness relies entirely on the bridge's lock | Requests serialised by the library |
| C14/U10: reply not matched to request (transaction id 0 unchecked) | Silent wrong values possible if requests interleave | pymodbus and tmodbus match transaction ids |
| C15: reconnect not thread-safe | Unsafe with a concurrent reader | Async, single owner; unit handles survive recycles |
| P3/U9: any error text containing "modbus" triggers reconnect and rescan | Yes | Typed exceptions; a device exception reply is not a dead link |
| C10: a second socket for vendor registers | Separate pymodbus client | Plain register reads on the same connection |
| C11: a library timer thread writes outside the lock | Yes (`threading.Timer` → `reset_control_state`) | No timer. The watchdog is the caller's, as in §5.2. |
| Layout drift | Not detected | `SunSpecMapShiftError` on a header mismatch |
| C16: aGate closes connections mid-request | Seen as "Response timeout" and a reconnect storm | **Unknown.** A persistent connection may behave better or worse on this aGate; untested (U1–U3). |
| Multiple clients sharing :502 (bridge, Local Bridge) | Not addressed | **Not addressed.** It serialises within one process only. |

### 11.3 Pros

- **It is exactly the transport this design asks for** in §5.2: one owned,
  serialised, persistent connection per device. It is also the pattern of
  Home Assistant's own ModbusHub.
- **It removes the library-side causes in one move:** C1, C3, C10, C14, C15,
  P3/U9 and the reply-mismatch risk (U10).
- **The bridge is already async,** so it fits better than a synchronous library
  run in threads.
- **SunSpec support** brings layout-drift detection and spec-generated enums
  (V1.2, 0-based), consistent with #51 and #55.
- **Maintained by the organisation behind Home Assistant,** and Apache-2.0.

### 11.4 Cons and risks

- **Young and fast-moving:** four major versions in about 3.5 months. It needs
  an exact pin and an upgrade budget.
- **It does not solve the shared port:** the bridge and the Local Bridge both
  using :502 still needs one owning process or a proxy (§9.3).
- **Control logic would have to live somewhere.** The M704 write sequence and
  pacing, dispatch verification (`verify_dispatch` semantics), the command
  watchdog, orphan release and the documented FranklinWH quirks
  (`FRANKLINWH_SUNSPEC_QUIRKS.md`, `SUNSPEC_ENUM_BASIS.md`,
  `CONNECTION_AND_THREADING.md` in franklinwh-modbus) all come from
  franklinwh-modbus today.
- **Conflicts with a project rule if the bridge adopts it alone.** CLAUDE.md:
  "The franklinwh-modbus library handles all Modbus register I/O. This project
  wraps it; it does not re-implement Modbus." The owner would have to change it.
- **Divergence:** the Local Bridge also uses franklinwh-modbus. A bridge-only
  switch would split the two bridges' transports, against the align-by-default
  rule.
- **Untested on this aGate:** whether it tolerates one long-lived connection is
  unknown (C16, U1–U3).

### 11.5 Recommended path, if and when it is taken up

1. **First,** run Phase 0b (baseline) and experiments E1–E5 from this design.
2. **Then a read-only trial in a coordinated window:** a minimal
   modbus-connection poller reads the bridge's registers (the SunSpec models
   and 15000–15039, 15510–15513, 16000–16002) with the bridge's poller paused.
   Compare connection closes, "Response timeout" against "timed out", latency
   and failures with the baseline.
3. **If it wins:** adopt it **inside franklinwh-modbus**, as the owner's
   forked-library roadmap item. The library exposes an async API that keeps the
   dispatch semantics. The bridge, and later the Local Bridge, consume it, so
   the project rule and cross-bridge alignment hold.
4. **Not recommended:** the bridge switching transports on its own (§11.4).
5. **Independent of all this,** Phase 1 (truthful dispatch) proceeds on the
   current library, because it is needed whatever the transport.

### 11.6 Roadmap option (owner, 2026-10-12): Bridge 2.0 on FranklinWH Modbus API 2.0

The owner's roadmap direction is not a retrofit but a parallel line:
- **Bridge 2.0:** a fork of this bridge;
- **FranklinWH Modbus API 2.0:** a fork of franklinwh-modbus, built on
  modbus-connection;
- 2.0 consumes the API 2.0; Bridge 1.x stays on the current library.

Points to settle when it starts (none is decided here):

- **Bridge 1.x stays the production line** until 2.0 passes the same proof as
  this design (§6, §7): the baseline comparison, emulator fault injection,
  T1–T7, and live acceptance. Fixes in this design (Phases 0b–2) land in 1.x.
  2.0 inherits the requirements, not necessarily the code.
- **One client per aGate still applies.** 1.x and 2.0 must not poll the same
  aGate at the same time outside coordinated windows. Develop 2.0 against the
  emulator, or a second aGate, or run it with 1.x's poller paused.
- **Carry over the semantics, not just the transport:**
  - the dispatch states and verification (§5.1);
  - the M704 sequence and pacing, with E6's finding on re-sends;
  - the watchdog and orphan release;
  - the documented FranklinWH quirks;
  - the V1.2 enum basis (#51, #55);
  - the measurement schema (§5.4), so 1.x and 2.0 are compared on the same
    numbers.
- **Coexistence with Home Assistant.** 2.0 must not collide with 1.x on MQTT
  topics, entity `unique_id`s or command topics (see the namespace-collision
  risk). It needs a distinct prefix or an explicit migration path for existing
  HA entities and the database.
- **Alignment: decided by the owner, 2026-10-12,** and told to fwh-modbus
  (API 1.0). The **Local Bridge (Direct Connect)** and the **Energipays
  Bridge** will also use FranklinWH Modbus API 2.0, as forked new versions.
  API 2.0 is the shared Modbus layer for all three bridges' 2.0 lines.
  - **The gateway role is provided for in 1.x (§5.5.1)** and built as a future
    branch (§5.5.2), so 2.0 inherits a proven shape rather than inventing one.
  - **Single owner, decided by the owner, 2026-10-12: Modbus Bridge 2.0 is the
    gateway.** It is the only process holding a Modbus connection to each
    aGate. Local Bridge 2.0, Energipays 2.0 and any other consumer reach the
    aGate's Modbus data and control **through Modbus Bridge 2.0**, never by
    opening :502 themselves. This closes the multi-client gap: API 2.0
    serialises requests only within one process.
  - **Existing hook:** the Local Bridge container already has a
    `MODBUS_BRIDGE_URL` setting (empty on 2026-10-12), which suggests it was
    built to consume this bridge.
  - **To design when 2.0 starts (open; not decided here):**
    - **The interface other bridges use.** REST, which this bridge already
      treats as canonical; and/or MQTT; and/or a Modbus TCP proxy endpoint
      for unmodified clients.
    - **Command arbitration and priority** between consumers, e.g. a schedule
      dispatch versus another bridge's command. One dispatch state model
      (§5.1), so every consumer sees the same verified, failed or
      not-effective result.
    - **Read fan-out:** consumers share the owner's poll rather than causing
      extra aGate reads, with a staleness or age field on every value (§5.2).
    - **Authentication** between the bridges, and an audit trail of which
      consumer sent which command.
    - **Availability.** If Bridge 2.0 is down, the consumers have no Modbus
      access. Decide whether that is acceptable or needs a fallback.
