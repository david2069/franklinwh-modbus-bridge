# Bridge Catalogue — failure modes a FranklinWH bridge can have

**This is a catalogue, not a contract.** It names behaviours worth having, gives each a
stable number so findings can be referenced across repos, and records where each bridge
stands. **No bridge is obliged to satisfy any of it.** Several entries have more than one
defensible answer, and where they do, the entry says so rather than picking for you.

It started life as a conformance contract and that was the wrong shape. The two bridges
speak different protocols, have different capabilities, and are at different stages — the
Modbus Bridge is ahead on orchestration, so "conform to the other one's requirements" had
it backwards. The first real test failed it: BR-18 asserted that a stopped rule must be
resumable, while the Modbus Bridge deliberately makes Stop terminal, with the reasoning
written into its own audit message. A contract one party knowingly fails on day one is not
a contract; it is one team's design imposed on another's product.

What does work, demonstrably, is narrower and cheaper:

* **a shared vocabulary**, so one machine is not described two ways —
  [`WHAT_THE_ENGINE_IS.md`](WHAT_THE_ENGINE_IS.md);
* **findings that travel**. "We found X — do you have it?" On 2026-10-09 that exchange
  found a real defect in the Modbus Bridge, which its maintainer then **corrected and
  sharpened** within the hour: the unguarded task was the child MQTT listener, not the
  publisher loop, and its failure mode is worse than the one originally reported — inbound
  commands go deaf while state publishing continues, so nothing looks wrong.

There is no sync requirement and no drift checker. An earlier revision shipped
`tools/check_baseline_sync.py` to detect the two copies diverging; building a drift
detector was itself the clue that the premise was wrong. Copies may diverge. That is fine.

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
happens when nobody wrote the failure mode down. That is what this catalogue is for —
naming them once, so the second bridge does not have to rediscover them.

## Detection matters more than the list

A property you hold today and silently lose tomorrow was never really held. Anything here
worth having is worth noticing the loss of —
see [`OBSERVABILITY_COVERAGE.md`](OBSERVABILITY_COVERAGE.md), which maps each `BR-n`
to the check that detects it, the log line it writes, and whether it notifies.

That companion exists because most failures in this list produce **no event**: an
orphaned entity raises nothing, a wrong version number raises nothing, a burned
schedule occurrence writes one line and moves on. Notifications are event-driven and
cannot cover standing conditions, so a periodic self-check is part of the contract,
not an extra.

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

> These requirements only make sense once you know the engine is a *thermostat* rather than
> an *alarm clock* — see [`WHAT_THE_ENGINE_IS.md`](WHAT_THE_ENGINE_IS.md). BR-15–21 are what
> that implies, written as obligations.


**BR-15** A scheduled occurrence fires at most once on success.

**BR-16** A scheduled action that fails transiently is retried **within its window**, and
the occurrence is not marked complete until it succeeds or the window closes.

**BR-17** A window whose start has passed — because the bridge was down, or busy — is
still entered if the window is open, for the time remaining.

**BR-18** Stopping a running rule has **two defensible answers, and a bridge should pick
one deliberately rather than inherit it.**

* **Terminal** — Stop ends the occurrence; it will not re-enter this window. Unambiguous,
  no second state to reason about, and an operator who stopped something probably meant it.
  *The Modbus Bridge chose this, explicitly: its audit line reads "stopped by user —
  released; won't re-fire until the next window", with extra code to block a between-ticks
  re-fire.*
* **Resumable** — Stop pauses; the rule may resume while its window is open. The remaining
  time is often the valuable part — a four-hour export window stopped after thirty minutes
  loses three and a half hours of tariff opportunity that does not come back until tomorrow.

What is **not** defensible is neither: the Direct Connect Bridge currently releases the
dispatch but leaves the "already fired" marker set, so it cannot re-enter *and* nothing
records the decision. That is not a third choice, it is the absence of one.

A bridge offering both should make them **separate verbs** — *Stop* (terminal) and *Pause*
(resumable) — so the meaning is stated rather than inferred.

**BR-19** "Run now" and "resume" are distinct operations with distinct semantics. Run now
ignores the window; resume honours the time remaining in it.

**BR-20** A dispatch interrupted by a restart is reconciled on boot against a stated
policy, and a dispatch stopped deliberately is never resurrected by that reconcile.

**BR-21** Every force carries a watchdog owned by the bridge, independent of any
device-side revert timer, because the device-side timer may be cosmetic.

## 4b · Orchestration

**BR-44** Exclusivity is claimed on the **resource a rule drives**, never on the device as a
whole. A rule that only notifies, or that commands equipment the bridge does not arbitrate,
contends for nothing — otherwise a constantly-evaluating notify-only rule holds the device
and starves every other rule on it while doing no work itself.

**BR-45** A rule targets **one device or all of them**, and ownership is keyed per device so
it stays stable when the member set changes. Adding a device to a group must not silently
transfer or void an existing claim.

**BR-46** Conflicts resolve by **priority with a deterministic tie-break**, so identical
inputs always choose the same winner. A rule added later never displaces an established one
at equal priority. Losers are recorded as skipped **with the reason and the winner named** —
"why didn't mine run" must be answerable without reading logs.

**BR-47** Entry and exit are **symmetric**: a rule may act on entry and on exit, and the
exit action runs even when the period is cut short — by a stop, a lost conflict, an
exception or a shutdown. An exit action that only runs on the happy path is worse than none,
because it will be trusted.

**BR-48** Notification text may be **templated from the same vocabulary as conditions** —
whatever a rule can be gated on can be quoted in its message, with no second alias table to
drift. A value that cannot be resolved renders as a placeholder rather than failing the
send: a message with a gap beats no message, because notifications are what you reach for
when something has already gone wrong.

> Logging obligation that runs through all of these: **event** (it ran, was skipped, lost,
> was stopped), **error** (it tried and failed) and **exception** (the engine itself
> misbehaved) are three different things with three different audiences, and must not share
> one undifferentiated stream.

**BR-49** A rule declares **what should happen if it was missed** — resume if the period is
still open, or record it as missed. Per rule, because "catch up if you can" is right for a
discharge window and wrong for a one-shot alert. After an outage spanning several periods,
catch-up runs **once**, never once per period missed.

**BR-50** A rule that overrode a device setting **restores it on exit**, rather than leaving
the device wherever the override left it.

**BR-51** Rules are **exportable and importable** so they can be shared between installs,
and a shared rule carries its **definition only** — never its run history or in-flight state.

**BR-52** A rule preview states **what it cannot prove**. "Conditions hold now" is not
"this will run": a continuous-hold requirement cannot be confirmed from a single instant,
and the window may not be open. A green verdict that will be trusted must name its own
limits.

**BR-53** Condition results are reported **with the structure that produced them** — a
verdict per group, the match mode, and the overall result — not a flat list. Under an ANY
match a failing row is not a failure, so flat per-row pass/fail misleads on exactly the
nested rulesets that need explaining. A value that cannot be read is reported as its own
third state, distinct from false.

**BR-54** A preview can be scoped to **one rule or all of them**, because "why is nothing
happening?" is usually answered by a conflict or a priority loss elsewhere, not by the rule
being inspected.

**BR-55** Continuous-hold state either **survives a restart**, or a restart **records that
it was discarded**. A hold silently reset by a restart can never complete on a system that
restarts more often than the hold is long. Starting a hold and *abandoning* one are both
events; the abandonment is the informative one.

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

## 8 · Runtime and supervision

A requirement can only hold while the thing that implements it is running. Both bridges
create long-running background work and neither supervises any of it: no heartbeat, no
exception retrieval, no restart policy, no observable worker state. Safety is by
convention — every author remembering a `try/except` — which is a streak, not an
architecture, and in the Modbus bridge the streak is already broken in two of five
long-running loops **including the health component itself**.

**BR-35** Every unit of background work is a named worker with **one** concern, a declared
restart policy and an owner. No background work exists outside the registry — not a bare
task, not a daemon thread.

**BR-36** Liveness is **proven, not inferred**. A worker beats on each cycle; a stale
heartbeat means failed even when the task object is alive and the process is healthy. "The
task is not done" is not liveness.

**BR-37** A worker that exits never exits unreported: its exception is retrieved and
recorded. Restarts follow the policy with backoff, bounded by a crash-loop ceiling — on
exceeding it the worker stops and says so, because a silent restart loop is worse than a
stopped worker.

**BR-38** Worker state is observable **from outside the worker** — name, scope, state,
uptime, last beat, restart count, last error — and `degraded` is distinct from `failed`.
A poller that cannot reach its device is working correctly and reporting a device problem;
conflating the two makes "gateway unreachable" and "poller crashed" the same silence.

**BR-39** The lifecycle distinguishes states that imply different remedies: an
intentional stop from a failure, a **wedged** worker (alive, not beating — cancel, then
restart) from a **crashed** one (already gone — restart), and **paused** (idle, state
retained, resumable) from **stopped** (torn down). Stopping is a state with a duration,
not an instant, so a stop that never completes is itself detectable.

**BR-40** A worker is deregistered only when its work has **actually ended**, and a name
cannot be reused while a predecessor is still winding down. Otherwise a stop that returns
before its task exits lets a replacement start alongside it — two components doing the same
job, the older one invisible because it has already been deregistered.

**BR-41** Health is reported at three levels — **mechanism** (is this worker alive),
**capability** (what can the bridge do now), **impact** (what does that mean for what the
user configured) — and impact is **derived from declared dependencies, never from a
criticality flag on a component**. The same component failing means different things as the
architecture around it changes; a hardcoded flag silently stops being true.

**BR-42** Capability health includes **data freshness**, not only component liveness. A
component can beat steadily while every operation inside it fails, and work evaluated
against stale inputs is a distinct hazard that liveness cannot express.

**BR-43** Scheduled work is **pre-flighted** before its window: a schedule that cannot run
is reported as such in advance, with the reason. A pre-flight failure is **skipped with a
reason**, which is not the same as failed and must not be retried as though it were
transient.

> A corollary of BR-35 that is easy to miss: the health, self-check and notification
> components are themselves workers, and must be supervised by something other than
> themselves. Observability cannot live inside the thing it observes.

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
| 4 · Scheduler & windows | BR-15/17/20/21 hold; **BR-16, BR-19 do not**. BR-18: **neither answer** — releases without recording, cannot re-enter | BR-18: **terminal, deliberately** (see the entry). Others unassessed |
| 4b · Orchestration | **BR-44–48 fail.** Priority and conflict fields exist but there is no resource exclusivity, no deterministic winner, no templated notifications. Phase 2 adopts the Modbus Bridge's model. | **Passes BR-44–46 and BR-48** — per-resource lanes, one-or-all targeting with stable ownership, `winner()` by priority then age, `%sensor.id%` templating sharing the condition vocabulary. The reference implementation. |
| 5 · Entity lifecycle | BR-22/25/26 pass; **BR-23 fails** — deleting a gateway leaves its retained discovery configs behind. BR-24/27 **fail**. | BR-23 **passes** — `DELETE /api/gateways/{id}` cascades device_points → device_models → gateway_state → metrics → metrics_archive → row, leaving no orphans. BR-27 **passes**: mock data is never recorded. |
| 6 · Observability | BR-29/31 pass; BR-28/30 arriving with the outcome wiring | BR-29 passes (per-gateway lifecycle events, control_log); BR-26 partial — per-gateway row counts need SQL (§7.2) |
| 7 · Write gating | BR-32/33 pass; BR-34 **fails** where BR-3 does | — |
| 8 · Runtime, supervision & health | **BR-35–43 all fail.** BR-40 concretely: `stop_poller` returns before its task exits while `is_running()` reads a dict already popped, so a quick disable→enable runs two pollers for one gateway. The scheduler is a passenger on the gateway poll loop; `run_gateway` has no `except`, so one exception silently ends polling, metrics, MQTT and all scheduling for that gateway. | **BR-35–38 all fail** for supervision — ~15 `create_task` sites, none watched. But its scheduler **is** a proper component with its own task and tick, and 3 of 5 loops guard themselves. The Direct Connect Bridge should adopt that shape. |

### Where to copy from, rather than re-solve

* **BR-23 / BR-27 — the Direct Connect Bridge should copy the Modbus bridge.** Its
  delete already cascades every artefact a gateway produced, and its mocks never write
  metrics at all. The Direct Connect Bridge deletes the row and leaves retained MQTT
  discovery configs behind, which is how it accumulated orphaned devices.
* **BR-35–38 (scheduler shape) — the Direct Connect Bridge should copy the Modbus
  bridge.** Its `gateway/scheduler.py` already owns its own task and tick interval, with
  an inner guard commented "never let one bad tick kill the loop". That is exactly phase 1
  of `RUNTIME_DESIGN.md`, already written and running next door. The supervision layer
  (BR-36–38) is missing from both and has to be built once.
* **BR-1 / BR-4 — the Modbus bridge should copy the Direct Connect Bridge.** It already
  runs one MQTT publisher per gateway keyed on the gateway's own serial, so two gateways
  cannot merge into one Home Assistant device, and metrics rows are tagged per gateway
  and filtered on query.

Keep this table current. A requirement nobody has assessed is the one that bites.
