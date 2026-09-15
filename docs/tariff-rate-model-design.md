# Tariff rate model — seasons, time periods, tiers

How the bridge answers "what does a kWh cost right now?", and why it is shaped
this way. Written 2026-09-15, after the model went in.

## The gap it filled

The bridge priced exports, demand charges and standing charges but had **no
import-rate model at all**. On a real AGL bill that left two of six rates
unrepresentable — and the one with the widest reach, the 3c base feed-in
tariff, had nowhere to be entered. Only the 28c evening bonus was known, so
every other exported kWh was valued at zero. "Cost this period" covered the
surcharges and omitted the energy.

## Shape

```
pricing.default_rate = { buy, sell }            # the tariff-wide rate
pricing.seasons = [
  { id, name,
    months: [11,12,1,2,3,6,7,8],                # [] = catch-all
    time_periods: { off_peak: {buy, sell}, … },
    blocks: [ {start, end, time_period, days?} ] }
]
```

`buy` and `sell` are each either a number or a tier ladder:

```
buy: [ {up_to_kwh: 1000, rate: 0.22}, {rate: 0.28} ]
```

## Decisions worth keeping

**The tariff-wide rate is the base; seasons override it.** A flat or tiered
tariff is complete with `default_rate` alone — no seasons, no time periods.
Making someone build a "season" to say "30c a kWh" had it backwards.

**Blocks name a time period rather than carrying prices.** A real plan's
import and export boundaries need not align: AGL's peak import runs 15:00–21:00
while its evening feed-in runs 17:00–21:00, so that span is two periods sharing
a buy price and differing on sell. Separate import-window and export-window
structures — what the bridge had — cannot express that without contradicting
themselves.

**Time periods are named, not price-derived.** AGL's 07:00–08:00 morning band
prices identically to off-peak and is still its own period, because the plan
names it. Never dedupe by price.

**Tiers live on the period, and count consumption at the METER.** They resolve
against `period_import_kwh` (the `grid_import_wh` counter) across the billing
period, regardless of which time period the energy fell in — which is what a
tiered plan bills on. It also makes hybrid free: a time period whose price is
tiered *is* a hybrid plan, with no third concept.

**"time_period", not "period".** FranklinWH's own term is Time Period, but this
codebase already uses "period" ~180 times for the BILLING period
(`period_start`, `billing_periods`, `demand.period_charge`). One word for two
concepts would be a lasting trap.

**Cost is integrated, not multiplied at the end.** A period spans seasons,
periods and midnight; the rate genuinely changes underneath it, so only a
running total prices each kWh correctly.

**A gap is reported, never guessed.** An hour no block covers falls back to the
tariff-wide rate if one is set; otherwise it resolves to
`reason="no_block_for_time"` with null rates and accumulates as *unpriced* kWh.
Inventing a price produces a confident wrong bill, which is worse than a
visible gap.

**Export is credited once.** The rate model's `sell` and the older
export-bonus window are two ways of saying the same thing — AGL's 28c bonus
covers 17:00–21:00 and so does `mid_peak.sell`. Counting both credited evening
export twice. The rate model wins once it prices export.

## Compatibility

Readers accept the pre-rename `waves` / `wave` keys. Profiles exported before
the rename are files already in users' hands, and an import that silently
priced nothing would be worse than the old name. Migration 43 rewrites stored
JSON; the tolerant read stays for imports.

## Energy Costs history

Closed periods snapshot retailer / network / plan_version (migration 40) and
energy cost/credit (41). The history table groups consecutive periods sharing
a provider and plan, with a per-group net total, so a retailer switch reads as
a boundary rather than blending two plans into one trend. Periods that closed
before migration 40 show as "provider not recorded" — accurate, rather than an
invented attribution.

*(That grouping shipped inside commit 259688d, a UI-fixes commit, because
`git add -A` swept it in — noted here so it is findable.)*
