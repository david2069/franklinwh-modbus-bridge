# Bridge Baseline — behaviour both FranklinWH bridges must satisfy

**Canonical copy:** `franklinwh-direct-connect-bridge/docs/BRIDGE_BASELINE.md`.
A verbatim copy lives in `franklinwh-modbus-bridge/docs/`. Edit the canonical one and
copy it across; `tools/check_baseline_sync.py` fails if the two drift.

## Why this exists

The two bridges share no code. They were written months apart, against different
transports, and they independently grew **the same defects**:

* battery control that ignored which gateway was selected, because a single-gateway
  assumption was hardcoded — found in the Modbus bridge by its owner, and found again
  in the Local Bridge on 2026-10-09 (`POST /api/dispatch` took no gateway parameter and
  resolved its target from global settings);
* a stopped schedule that could never resume, because stopping released the dispatch
  but left the "already fired this occurrence" marker set — true of both.

Two codebases reaching the same wrong answer separately is not coincidence. It is what
happens when the behaviour was never written down. This file is that statement: what a
FranklinWH bridge must *do*, independent of whether it speaks Direct Connect, Modbus
TCP, or the cloud API.

## How to use it

Requirements are numbered `BR-n` and phrased so a test can assert them. Reference the
number from the test that proves it:

```python
def test_dispatch_requires_an_explicit_gateway():   # BR-3
```

A bridge is **not** expected to satisfy every requirement immediately. It is expected to
know which ones it fails, and to have them in its backlog. An unassessed requirement is
worse than a failed one.

---

## 1 · Identity and multi-gateway

**BR-1** Every gateway-scoped operation — read, write, metric, entity — identifies its
gateway explicitly. A bridge supporting one gateway still names it.

**BR-2** No operation silently falls back to a "default", "first" or "only" gateway. If
a gateway is required and not supplied, the bridge refuses with an error naming the
gateway as missing.

**BR-3** Control actions that change physical state (mode, off-grid, dispatch, generator,
circuits) resolve their target from the named gateway, never from global configuration.

**BR-4** Two gateways never share an identity in any downstream system — distinct
entity ids, distinct device records, distinct metric rows, distinct log attribution.

**BR-5** A gateway whose identity is not yet known (serial unread) performs no action and
publishes no identity-bearing artefact. It waits and retries rather than using a
placeholder.

## 2 · Outcome semantics

**BR-6** Every outward call returns a structured outcome, not free text. Callers branch
on the outcome; they never substring-match a message.

**BR-7** The outcome distinguishes at least: **ok**, **failed** (it did not happen),
**unknown** (it may have happened), and **skipped** (preconditions unmet).

**BR-8** `unknown` exists and is used. A write whose request plausibly reached the device
before the failure is `unknown`, not `failed`. A failure that provably never left —
connection refused, name resolution failure — is `failed`.

**BR-9** An `unknown` write is never retried automatically. Retrying an ambiguous force
is how a battery gets dispatched twice.

**BR-10** Every consequential write records its outcome durably, including `unknown`.

## 3 · Deadlines and retries

**BR-11** Every outward call is bounded by a deadline covering the whole attempt,
including any backoff between retries.

**BR-12** Failures are classified transient or permanent. Permanent failures are not
retried; a rejection repeated is still a rejection.

**BR-13** Retries use bounded exponential backoff with jitter, so multiple gateways do
not retry in lockstep.

**BR-14** A slow or unreachable device degrades one operation, never a whole cycle. A
poll loop bounds its own work.

## 4 · Scheduler and window semantics

**BR-15** A scheduled occurrence fires at most once on success.

**BR-16** A scheduled action that fails transiently is retried **within its window**, and
the occurrence is not marked complete until it succeeds or the window closes.

**BR-17** A window whose start has passed — because the bridge was down, or busy — is
still entered if the window is open, for the time remaining.

**BR-18** A running schedule can be stopped, and a stopped schedule can be **resumed**
while its window is still open. Stopping does not consume the occurrence.

**BR-19** "Run now" and "resume" are distinct operations with distinct semantics. Run now
ignores the window; resume honours the time remaining in it.

**BR-20** A dispatch interrupted by a restart is reconciled on boot against a stated
policy, and a dispatch stopped deliberately is never resurrected by that reconcile.

**BR-21** Every force carries a watchdog owned by the bridge, independent of any
device-side revert timer, because the device-side timer may be cosmetic.

## 5 · Entity and integration lifecycle

**BR-22** *Offline* and *removed* are different states. A device that is unreachable goes
offline and its entities remain; a device that is deleted has its entities removed.

**BR-23** Deleting a gateway removes the artefacts it published before the bridge forgets
it. Removal happens while the bridge still knows what to remove.

**BR-24** The bridge offers a deliberate "remove everything I published" action, for use
before uninstalling, because an uninstall does not run the bridge's own code.

**BR-25** Published artefacts are attributable to the publishing bridge, so a shared
broker or registry can be cleaned without touching another integration's records.

**BR-26** The bridge can report what it published, what is stale, and what belongs to
someone else — and any destructive cleanup offers a dry run first.

**BR-27** Test or mock devices are distinguishable from real ones, and tearing one down
removes what it published.

## 6 · Observability

**BR-28** Log severity follows the outcome. `unknown` is never logged at info; it is the
state a human must resolve.

**BR-29** Consequential writes are recorded in an audit trail separate from the log
stream, with actor, target, action and outcome.

**BR-30** Failed and unknown outcomes on writes raise a notification; successful ones do
not.

**BR-31** Observability never changes behaviour. A failing logger, audit sink or notifier
is swallowed, never propagated into the action.

## 7 · Write gating and safety

**BR-32** Writes are disabled by default and enabled deliberately.

**BR-33** A write that is unavailable reports **why** — missing capability, missing
credentials, unreachable transport — rather than failing opaquely or being hidden.

**BR-34** The bridge never presents a control it cannot perform on the selected target.

---

## Conformance status

Honest as of 2026-10-09, from reading both codebases. "—" means nobody has checked,
which is itself a finding.

The striking result: **each bridge passes what the other fails.** Neither is ahead
overall, and both have a working implementation of the other's gap to copy.

| Area | Direct Connect Bridge | Modbus Bridge |
| --- | --- | --- |
| 1 · Identity & multi-gateway | BR-3 **fails** — `POST /api/dispatch` takes no gateway and resolves the host from global settings. BR-5 fixed 2026-10-09. MQTT **passes**: one publisher per gateway, node = serial. | BR-1/4 **fail** for publishing — non-default gateways do not publish their own HA devices (their `multi-gateway-and-mock-lifecycle.md` §7.3). BR-4 also fails for metrics: the history chart merges all gateways (§7.1). Serial-collision detection planned (§7.4). |
| 2 · Outcome semantics | BR-6–10 implemented in `resilience.py`; wiring in progress | — |
| 3 · Deadlines & retries | BR-11–13 implemented; BR-14 partial (`DEF-POLLER-STALL`) | — |
| 4 · Scheduler & windows | BR-15/17/20/21 pass; **BR-16, BR-18, BR-19 fail** | BR-18 reported failing by the owner — a stopped task cannot resume |
| 5 · Entity lifecycle | BR-22/25/26 pass; **BR-23 fails** — deleting a gateway leaves its retained discovery configs behind. BR-24/27 **fail**. | BR-23 **passes** — `DELETE /api/gateways/{id}` cascades device_points → device_models → gateway_state → metrics → metrics_archive → row, leaving no orphans. BR-27 **passes**: mock data is never recorded. |
| 6 · Observability | BR-29/31 pass; BR-28/30 arriving with the outcome wiring | BR-29 passes (per-gateway lifecycle events, control_log); BR-26 partial — per-gateway row counts need SQL (§7.2) |
| 7 · Write gating | BR-32/33 pass; BR-34 **fails** where BR-3 does | — |

### Where to copy from, rather than re-solve

* **BR-23 / BR-27 — the Direct Connect Bridge should copy the Modbus bridge.** Its
  delete already cascades every artefact a gateway produced, and its mocks never write
  metrics at all. The Direct Connect Bridge deletes the row and leaves retained MQTT
  discovery configs behind, which is how it accumulated orphaned devices.
* **BR-1 / BR-4 — the Modbus bridge should copy the Direct Connect Bridge.** It already
  runs one MQTT publisher per gateway keyed on the gateway's own serial, so two gateways
  cannot merge into one Home Assistant device, and metrics rows are tagged per gateway
  and filtered on query.

Keep this table current. A requirement nobody has assessed is the one that bites.
