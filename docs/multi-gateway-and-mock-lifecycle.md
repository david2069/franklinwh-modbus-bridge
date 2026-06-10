# Multi-Gateway & Mock Gateways — Lifecycle, Data, Audit & Purge

**Status:** current as of branch `multi-gateway-and-ui-fixes`
**Audience:** operators/devs who need to know exactly what each gateway writes,
where it shows up, and how to clean it up.

---

## 1. Two kinds of gateway

| | **Real gateway** | **Mock gateway** (`mock=1`) |
|---|---|---|
| Connection | Modbus TCP to a physical aGate | **None** — no socket opened |
| Data source | Live SunSpec/extension registers | Synthetic curves ([`mock_gateway.synthetic_points`](../src/franklinwh_bridge/gateway/mock_gateway.py)) |
| Serial | Real device serial | `MOCK-<gateway_id>` (collision-free) |
| Purpose | Production | Demo / UI / multi-gateway testing without hardware |

A mock is selected with the **"Mock gateway"** checkbox in *Settings → Add Gateway*.
Code seam: [`GatewayInstance.start()`](../src/franklinwh_bridge/gateway/instance.py) branches to `_start_mock()` when `config.mock`.

---

## 2. Lifecycle (both types)

```
Add ──> Onboard (auto) ──> Poll loop ──> Stop ──> Delete
POST       registry          emits         POST     DELETE
/gateways  .start_gateway    samples       …/stop   /gateways/{id}
```

- **Add** — `POST /api/gateways`. Writes a `gateways` row. For a *real* gateway the
  add is rejected (409) if another **real** gateway already uses the same
  `host:port:unit` (the contention guard); mocks are exempt and need no host.
- **Onboard** — happens immediately on add (non-blocking) and on app start
  (`registry.start_all`). Real → connect + discover + capture catalog + poll.
  Mock → start `MockPoller` emitting synthetic samples every `poll_interval`.
- **Poll** — each sample is published to the gateway's own bus, fanned-in to the
  global bus, and consumed by the recorder / aggregator / MQTT (see §3).
- **Stop** — `POST /api/gateways/{id}/stop`. Stops the poller; **data is retained**.
- **Delete** — `DELETE /api/gateways/{id}`. Stops it **and purges its data** (§5).
  The `default` gateway cannot be deleted.

---

## 3. What each gateway persists — the data matrix

| Store | Real gateway | Mock gateway | Notes |
|---|---|---|---|
| **`metrics`** (Power History) | ✅ every poll | ❌ **never** | Mock skipped in `_record_metrics` ([main.py](../src/franklinwh_bridge/main.py)) |
| **`metrics_archive`** (rollup) | ✅ | ❌ | Derived from `metrics` |
| **`device_models` / `device_points`** (SunSpec catalog) | ✅ on connect | ❌ | Mock has no real device to read |
| **`control_log` / `operational_stats`** | ✅ | minimal | Audit/stats, tagged `gateway_id` |
| **MQTT HA Discovery** | **default only** | ❌ | Per-gateway publish (`register_device`) exists but isn't wired into startup yet — logs show "1 devices" |
| **Logs** (in-memory ring) | ✅ | ✅ | start/stop/poll events for all gateways |
| **Live dashboard / Site** (in-memory bus) | ✅ | ✅ | Not persisted — RAM only |

> **The key fact:** a **mock gateway never writes to `metrics`**, so **Power
> History can never contain mock data.** Mocks are visible *live* (dashboard when
> you select the mock, and the Site aggregate) but that comes from the in-memory
> sample bus and is never stored.

---

## 4. Where data appears (scoping)

| View | Endpoint | Shows |
|---|---|---|
| Dashboard — **Default** | `GET /api/points` | **default gateway's bus only** (scoped — no cross-gateway leakage) |
| Dashboard — a specific gateway | `GET /api/gateways/{id}/points` | that gateway's bus (incl. a mock's synthetic data) |
| Dashboard — **Site** | `GET /api/site/status` | aggregate of **all** gateways, live (incl. mocks) |
| **Power History** chart | `GET …query_metrics` | **all *recorded* gateways merged** → real gateways only (mocks aren't recorded) |

⚠️ **Power History is not yet gateway-scoped.** With **two or more *real*
gateways**, their recorded series merge on one chart. With one real gateway +
any number of mocks, the chart is clean (mocks aren't recorded). Per-gateway
charts are a planned enhancement (see §7).

---

## 5. Purge / cleanup — "how do I get rid of it?"

### A. Delete the gateway (the normal way)
`DELETE /api/gateways/{id}` (trash icon in *Settings → Gateways*) runs a
**cascade** ([`store.db.delete_gateway`](../src/franklinwh_bridge/store/db.py)):

```
device_points  →  device_models  →  gateway_state  →  metrics  →  metrics_archive  →  gateways row
```

So removing a gateway leaves **no orphaned catalog, state, or metric rows**.
For a **mock** there's essentially nothing to purge (no metrics/catalog) — delete
just removes the row.

### B. Retention (bulk, age-based)
`Settings → Metrics retention (days)` drives `purge_old`, which deletes `metrics`
+ `metrics_archive` older than N days across all gateways. Runs hourly.

### C. Manual (admin)
```sql
DELETE FROM metrics         WHERE gateway_id = '<id>';
DELETE FROM metrics_archive WHERE gateway_id = '<id>';
```
(Run inside the container; the DB is WAL-mode, so query the live file, not a
plain `cp` of `bridge.db` without its `-wal`.)

---

## 6. Audit & reporting

- **Logs tab** — filter by gateway, level, and source. Gateway lifecycle events
  (`started`, `stopped`, `unregistered`, `catalog captured`) are logged per
  `gateway_id`.
- **Settings → Database Storage** — row counts + time span per table
  (`metrics`, `metrics_archive`, `control_log`, …).
- **Per-gateway metric counts** — currently only via a DB query:
  `SELECT gateway_id, COUNT(*) FROM metrics GROUP BY gateway_id;`
  (A per-gateway breakdown in the Storage card is a candidate enhancement — §7.)

---

## 7. Known gaps / planned

1. **Gateway-scoped Power History** — make `query_metrics` take a `gateway_id`
   so each gateway (and Site) shows only its own series. Today the chart merges
   all *recorded* gateways.
2. **Per-gateway data report in the UI** — show metric/catalog row counts per
   gateway in the Storage card, and a "no mock data is recorded" affirmation, so
   the real-vs-mock split is visible without a DB query.
3. **Multi-gateway MQTT publishing** — wire `register_device` so non-default
   gateways publish their own HA devices (namespaced by `gateway_id`; mocks use
   `MOCK-<id>` serials so they can't clash).
4. **Serial-conflict warning** — flag two *real* gateways that discover the same
   serial (different host, same physical device).

---

## 8. TL;DR

- **Mock data is never recorded** → Power History is always real-gateway-only.
- **Live** views can show mock data (dashboard-when-selected, Site aggregate);
  that's RAM, not stored.
- **To purge a gateway's data: delete the gateway** — it cascades catalog +
  state + metrics. Mocks have nothing to purge.
- The chart only ever needs a "contains mock data" indicator if we later choose
  to *record* mocks; with the current design the honest indicator is **"never."**
