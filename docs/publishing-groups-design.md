# Publishing Groups — Design & Plan

**Status:** Draft for review · **Date:** 2026-06-09

Goal: let users publish *more* than the curated entity set to Home Assistant —
including SunSpec catalog points that aren't normally exposed — without
hand-writing `EntityDef`s, while keeping the curated set as the safe default.

This formalises the three modes raised in discussion:

1. **Default built-in models** — the curated entities, published as today.
2. **Customise models/points** — toggle which built-in entities publish.
3. **Add unpublished models** — promote arbitrary catalog points (all or
   selective) into publishing.

---

## 1. Where we are today

- **Curated entities** — ~61 `EntityDef`s in
  [`entities.py`](../src/franklinwh_bridge/publish/entities.py) (`BRIDGE_ENTITIES`).
  Each has a hand-tuned name, unit, device_class, state_class, scale/precision,
  and `source` (`mmm.ppp`). These are the only things that can be published.
- **Publishing groups** — [`groups_api.py`](../src/franklinwh_bridge/api/groups_api.py)
  + tables `publishing_groups` / `publishing_group_members`. A group bundles
  **curated entity slugs** and an enable flag; disabling a group unpublishes its
  members from MQTT Discovery. (Mode 2 already mostly exists.)
- **Full catalog** — `device_models` / `device_points` hold all 17 models /
  328 points with type/scale/symbols. The SunSpec Explorer browses these but
  **cannot publish** any of them.
- **Constraint** — [feedback-entity-patterns] says HA entities must be curated,
  not auto-mapped. Mode 3 deliberately relaxes this as an *opt-in* override.

So modes 1 and 2 exist; **mode 3 is the new work**, and it's the piece that ties
the Explorer to Publishing Groups.

---

## 2. Data model

Let a group member be one of **two kinds**, discriminated by `member_type`:

```
publishing_group_members
  group_slug    TEXT
  member_type   TEXT   -- 'entity' (curated slug) | 'point' (catalog ref)
  ref           TEXT   -- entity slug, OR "model.point" e.g. "705.VRef"
  -- for 'point' members, optional display overrides:
  name          TEXT NULL
  unit          TEXT NULL
  precision     INTEGER NULL
```

`member_type='entity'` is exactly today's behaviour (back-compatible —
existing rows migrate to `member_type='entity'`). `member_type='point'` is new.

A **promoted point** becomes a runtime entity built from catalog metadata:

| HA field | Source |
|---|---|
| `unique_id` | `franklinwh_<short_id>_m<model>_<point>` (uses the gateway-namespaced `short_id` from the serial-clash fix) |
| name | override → else point label from catalog |
| unit | override → else catalog point `units` |
| value | raw register × scale-factor (catalog `sf`), enum symbol if bitfield/enum |
| device_class/state_class | best-effort from unit (W→power, etc.), else none |
| ha_type | `sensor` (read), `number`/`select` only behind the RW gate (§5) |

These are clearly **raw/derived** entities — decent, not hand-tuned — and should
be labelled as such in the UI.

---

## 3. API

- `GET /api/catalog/points?access=R|RW&published=true|false` — list catalog
  points with metadata + whether each is already published (by any group or the
  curated set). Powers the Explorer "publishable points" view (and the
  Explorer ACCESS/PICS filter work).
- Extend group member endpoints to accept `member_type` + `ref` (+ overrides).
- `POST /api/groups/{slug}/points` — add one or more catalog points to a group.
- Discovery builder ([`mqtt_publisher.build_discovery_payload`](../src/franklinwh_bridge/publish/mqtt_publisher.py))
  gains a path that builds a payload from a promoted-point descriptor.

---

## 4. UI — Explorer ↔ Groups

This is where mode 3 lives, and it folds in the earlier Explorer asks:

1. In the **Explorer Schema view**, each point row gets a checkbox + an "Add to
   group…" action (multi-select supported). The **ACCESS / PICS filters**
   (Explorer item ③) make it easy to find publishable / supported points.
2. The **Publishing Groups** panel shows both kinds of members, with a badge
   distinguishing `built-in` vs `raw point`, and inline name/unit overrides for
   raw points.
3. A group's three "sources" map to the user's three modes: built-in entities
   (mode 1/2) and promoted points (mode 3) coexist in one group.

---

## 5. Safety & correctness

- **Read-only first.** Promote `access=R` points freely. **Gate `RW` points**
  (which would become HA `number`/`select` controls) behind an explicit warning
  — many aGate RW registers are broken/no-op/unsafe per [reference-vendor-issues].
  Recommend shipping mode 3 **read-only**, RW promotion as a later, guarded step.
- **No unique_id collisions.** Promoted-point ids are namespaced by the same
  gateway-aware `short_id` used for curated entities, and prefixed `m<model>_`
  so they can't clash with curated slugs or across gateways.
- **Scale factors / enums.** Apply the catalog `sf` and symbol maps so raw
  values are physically meaningful, not raw registers.
- **Multi-gateway.** Promoted points publish per-gateway like curated entities;
  group membership is global (point ref), resolved against each gateway's catalog.

---

## 6. Phased plan

- **P1 — Schema + read-only promotion (backend).** Migration for `member_type`;
  catalog-points API; discovery builder path for promoted read-only points;
  tests (payload shape, scale/enum, id namespacing, no collision with curated).
- **P2 — Explorer↔Groups UI.** Point checkboxes + "Add to group", ACCESS/PICS
  filters, raw-point badges + overrides in the Groups panel.
- **P3 — RW promotion (guarded).** Allow promoting writable points to
  number/select with a hard warning + per-point confirmation; respect existing
  command-handler clamping/watchdog.

P1 + P2 deliver the user's "add unpublished models, all or selective points"
read-only; P3 adds control for the brave.

---

## 7. Open questions for review

1. **Default for mode 3 entities:** publish disabled-by-default (opt-in per
   point) vs enabled when added to an enabled group? (Recommend: added = intended
   to publish.)
2. **RW scope:** ship read-only only in v1, or include guarded RW from the start?
3. **Override depth:** name/unit/precision only, or also device_class/icon?
4. **Doc vs build:** is this plan enough to start P1, or do you want a UI mock first?
