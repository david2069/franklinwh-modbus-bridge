# Multi-Phase Gateways & Electricity Services — Design (Backlog)

**Status:** Backlog / requirements capture — **not scheduled.** Logged so the
detail isn't lost; do not start until Publishing Groups and current work close.

**Primary purpose: coordinated control.** The service / amperage / phase
association is mainly a **logical grouping for coordinating battery control** —
running **charge / discharge / standby / release** across the grouped gateways
together (e.g. all aGates on one 3-phase service, or per phase). Per-phase data
mapping and reporting are secondary benefits of the same grouping.

**Context:** AU/NZ commonly run **3-phase** services; a site may have multiple
electricity services, and an aGate can be wired to **all phases** or a
**specific phase** (L1/L2/L3). The Explorer screenshots show SunSpec **701
(DER AC Measurement)** carries per-phase points (`WL1/WL2/WL3`, `VL1/VL2/VL3`,
`TotWhInjL1/L2/L3`, `VarL1…`, etc.).

---

## 1. What already exists (foundation)

- **`gateways.ac_type`** — detected wiring (0 single / split / three).
- **`site_config.ac_service_type`** — single site-level service type.
- **EntityDef `phase` gating** — L1/L2/L3 entities publish based on AC type
  (`phase=1` single/split/three, `=2` split/three, `=3` three only).
- **Model 701 per-phase registers** — the raw per-phase data is already
  readable (and now promotable via Publishing Groups P1).

What's missing is **multiple services**, **per-service ratings**, and an
explicit **gateway → service → phase** association.

---

## 2. Requirements (from review)

There are **two layers**, and keeping them separate is the key design idea:

- **Layer 1 — Electricity Utility Service(s): customer-declared, informational.**
  What the *customer understands* about their supply. A site can have **more
  than one** utility service / meter / AC type. None of this is derived from
  the device — it's reference data the customer enters.
- **Layer 2 — Gateways: the physical link.** Each gateway is linked *back* to a
  declared service/meter and tagged with the phase(s) it's actually wired to.
  This is where the informational layer becomes actionable (per-phase mapping,
  coordinated control, utilisation vs rated amps).

### A. Electricity Utility Service card  *(today's "Site Configuration")*
Rename/expand the card to reflect that it describes the customer's **utility
service(s)**, not the device. Per **service** (repeatable — a customer may have
several):
1. **Service ID** (label) + **Meter Number** + **Account** (informational).
2. **AC Service** — the customer's understanding of the service type
   (single / split / three-phase). *Customer-declared*, distinct from any
   device-detected `ACType` (see §4.2).
3. **Rated service amperage** (e.g. 63 A, 100 A) — needed for utilisation.

Site-wide fields (Site Name) stay at the site level; everything above moves to
a per-service list.

### B. Gateways (the linkage)
1. **Link a gateway to a declared service / meter** (Meter Number / AC Service
   / Amperage from Layer 1).
2. **Tag the gateway's wired phase(s)** — `L1`, `L2`, `L3`, or a combo —
   **constrained by its detected `701.ACType`**: single-phase (ACType 0) → one
   of L1/L2/L3; split → a two-leg combo; three-phase → all three.
3. The gateway's "Grid/Home/Solar power" then resolves to that phase's 701
   registers (`WL2`, `VL2`, `TotWhInjL2`, …) rather than the aggregate.

---

## 3. Proposed data model

```
services (Layer 1, customer-declared)   gateways (new columns, Layer 2)
  id            TEXT PK                    service_id  TEXT  → services.id
  name          TEXT  -- Service ID        phase       TEXT  'all'|'L1'|'L2'|'L3'
  meter_number  TEXT                                         | 'L1+L2' | …
  account       TEXT
  ac_service    INTEGER -- declared type
  rated_amps    INTEGER
  -- site_config keeps site-wide fields; services is the per-service list
```
- Migrate the single `site_config.ac_service_type` into a **`services`** table
  (one row per service) carrying `meter_number`, `account`, `ac_service`
  (declared), and `rated_amps`. Keep `site_config` for site-wide metadata
  (Site Name).
- `gateways` gains `service_id` + `phase`. `phase='all'` = today's behaviour.
- `phase` accepts a combo (`'L1+L2'`) for split/three-phase units; validate the
  choice against the gateway's detected `701.ACType`.

---

## 4. Behaviour

### 4.0 Coordinated control (the main driver)
- A **service (or phase) group** is a set of gateways controlled **together**.
- Control commands — **charge / discharge / standby / release** — can target a
  **group** and fan out to every member gateway's command handler, instead of
  driving each aGate one at a time.
- Coordination rules use the group's metadata: e.g. respect each service's
  **rated amperage** when charging/discharging the group, balance across phases,
  and release the whole group together (one watchdog/timeout per group).
- Builds on the existing per-gateway `CommandHandler` (charge/discharge/release
  + watchdog) — the group layer dispatches to N handlers and aggregates results.
- Open question: group-level command UI + how partial failures (one gateway
  rejects) are surfaced.

### 4.1 Per-phase data
- A gateway with `phase='L2'` maps its primary power/energy points to the **L2**
  variants from 701 (`WL2`, `VAL2`, `VarL2`, `AL2`, `VL2`, `TotWhInjL2`,
  `TotWhAbsL2`, …). EntityDef gains a phase-substitution so `701.WLn` resolves
  to the gateway's assigned phase.
- The **Site aggregate** sums across services/phases as configured.
- Per-service **rated amperage** enables overload/utilisation reporting
  (current vs rating) — and pairs naturally with the Reporting/Sankey backlog.
- Constrain phase choices to the gateway's detected `ac_type` (can't pick L3 on
  a single-phase unit).

### 4.2 Declared vs detected, and phase auto-detection
Two independent notions of "AC type" coexist — keep them distinct:
- **Declared** (`services.ac_service`) — the customer's understanding of the
  *service* (Layer 1). Informational.
- **Detected** (`gateways.ac_type` from `701.ACType`) — what the *aGate* reports
  about its own wiring (e.g. ACType 0 = Single Phase).

The gateway's **wired phase(s)** can be **auto-detected from 701 per-phase
registers**, then offered to the user to confirm/override (and used to flag
mismatches against what they declared):

| Signal | Tells you | Strength |
|---|---|---|
| `VL1`/`VL2`/`VL3` (phase-N voltage) | phase is **connected/energised** (~230–240 V vs 0) | **Best presence signal** — present even at zero power, no flicker |
| `TotWhInjLn` + `TotWhAbsLn` (lifetime energy) | phase has **carried energy** | **Best utilisation signal** — monotonic, immune to transient nulls |
| `WLn` / `ALn` (instantaneous) | live power now | weak alone — legitimately 0 on a live phase between loads |

Heuristic: **connected** if `VLn > 0`; **utilised** if `TotWhInjLn + TotWhAbsLn
> 0` (or growing). Example (real unit): `VL1`/`AL1`/`TotWhInjL1` non-zero while
all L2/L3 = 0 ⇒ single-phase on **L1**, consistent with detected `ACType=0`.
Per-phase `ALn` vs the service's **rated amperage** then gives utilisation %.

---

## 5. UI

- **"Site Configuration" card → "Electricity Utility Service(s)"** — a
  **Services** list editor (add/remove service; Service ID, Meter Number,
  Account, declared AC Service, rated amps) replacing the single AC-service
  dropdown. Site Name stays as a site-wide field.
- **Gateways** table/edit form → **Service/Meter** selector + **Phase** selector
  (All / L1 / L2 / L3 / combo, gated by detected ACType), with an
  **auto-detect** affordance (§4.2) that pre-fills the wired phase from 701 and
  warns on a declared-vs-detected mismatch.

---

## 6. Phasing

| Phase | What |
|---|---|
| MP1 | `services` table + `gateways.service_id`/`phase` (migration) + site/gateway API |
| MP2 | Phase-substituted entity resolution (gateway phase → 701 Ln points) |
| MP3 | Services editor + gateway service/phase selectors (UI) |
| MP4 | Per-service amperage utilisation in Reporting |

## 7. Open questions

1. Migrate `ac_service_type` → `services`, or keep both (site-type + services)?
2. Does a single physical aGate ever report *all* phases of a 3-phase service,
   or is it one aGate per phase? (Drives whether `phase` is per-gateway or
   per-entity.)
3. ~~Phase association manual, or inferred from 701 Ln registers?~~ **Resolved
   (§4.2): auto-detect from `VLn` + `TotWhInj/AbsLn`, user confirms/overrides.**
4. Can a customer have multiple meters on one service, or is meter 1:1 with
   service? (Affects whether `meter_number` lives on `services` or its own table.)
