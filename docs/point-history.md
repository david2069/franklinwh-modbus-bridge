# Point History

Settings → Data → **Point History**. Opt-in recording of the electrical readings
that `metrics` does not keep.

## Why it exists

The bridge has two history stores:

| Store | What it keeps | On by default |
|---|---|---|
| `metrics` | Battery, grid, solar and home power (W), SoC, temperatures, mode, grid mode, reserves. One row per poll. | Yes |
| `metric_samples` (Point History) | Per-point readings: AC voltage, current, frequency, power factor, **VA**, var, grid W, DC power, battery DC power/voltage/current, SoH, SoC, temperatures. One row per point per interval. | **No** |

`metrics` is small and fixed-width, so it carries only the figures the dashboard
and energy charts need. Everything else the poller reads is live-only unless
Point History records it. Without it:

- the AC and DC charts can show only what has happened since the page was opened;
- an export test cannot be checked in **VA** (kVA limits), only in W. The T4
  export test on 2026-10-10 hit this: `metric_samples` was empty, so the
  6.66 kW peak could not be stated in kVA.

## Why it is off by default

At the defaults (all 15 points, every 10 s, 21 days) it projects to about
250 MB, against a database that is otherwise about 20 MB. A bridge running as an
HA add-on on an SD card must not start using that because it was upgraded. The
card shows the projected size for the current settings before you save.

## Using it

- The **switch** saves immediately, together with anything else changed on the card.
- **Sample every**, **Keep for** and the point chips need **Save**. The card
  shows "Unsaved changes" until they are saved.
- Changes take effect on the next poll (the recorder re-reads its settings at
  most every 60 s). Nothing restarts.
- Lowering retention deletes nothing at once; the hourly maintenance pass purges
  rows older than the new limit.

Per-phase (L1/L2/L3) points are not recorded; add them to
`HISTORISED_POINTS` in `store/point_history.py` if a split- or three-phase site
needs them.

## API

- `GET /api/point-history/config`, `PUT /api/point-history/config` (control capability)
- `GET /api/point-history/series?points=grid_va,current_a&start=&end=`
- `GET /api/point-history/storage`
