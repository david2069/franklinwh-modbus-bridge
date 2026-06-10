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

### A. Site Configuration
1. **Number of electricity services** + a **Service ID** per service.
2. **Rated service amperage** per service (e.g. 63 A, 100 A).

### B. Gateways
1. **Associate a gateway to a service.**
2. **Associate a gateway to a phase**, constrained by its detected AC type.
3. **Link a gateway to all phases, or a specific phase** (L1 / L2 / L3) when
   it's wired that way — so its "Grid/Home/Solar power" reflects that phase's
   701 registers (`WL2`, `VL2`, `TotWhInjL2`, …) rather than the aggregate.

---

## 3. Proposed data model

```
services                          gateways (new columns)
  id           TEXT PK              service_id  TEXT  → services.id
  name         TEXT                 phase       TEXT  'all' | 'L1' | 'L2' | 'L3'
  rated_amps   INTEGER
  -- site_config keeps site-wide fields; services is the per-service list
```
- Migrate the single `site_config.ac_service_type` into a **`services`** table
  (one row per service), with `rated_amps`. Keep `site_config` for site-wide
  metadata.
- `gateways` gains `service_id` + `phase`. `phase='all'` = today's behaviour.

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

---

## 5. UI

- **Site Configuration** card → a **Services** list editor (add/remove service,
  name, Service ID, rated amps) replacing the single AC-service dropdown.
- **Gateways** table/edit form → **Service** selector + **Phase** selector
  (All / L1 / L2 / L3, gated by AC type).

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
3. Phase association manual, or inferred from which 701 Ln registers are non-zero?
