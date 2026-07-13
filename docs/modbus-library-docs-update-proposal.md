# franklinwh-modbus documentation update proposal

**Status:** Proposal only. `franklinwh-modbus` (`/Users/davidhona/dev/modbus`) is
read-only from this bridge session — apply these from a dedicated session for
that repo, per the multi-session repo workflow.

**Origin:** User review 2026-07-13, prompted by a suspicion that the library's
docs still promote a hardware timer that's confirmed broken, plus two rounds
of extension-register exploration (SunSpec Explorer JSON export + a live
`--match`-based raw register read against the device).

---

## A. Stale "WORKS"/"FUNCTIONAL" claims for `WSetRvrtTms` (safety-relevant)

The library's *primary* docs are correct and current: `readme.md:69`,
`docs/PICS_CONFORMANCE_CROSS_REFERENCE.md:65,307`, `docs/SAFETY_CONTROLS.md:
356-379`, and `controller.py:78,1417` all consistently state the hardware
auto-revert (`WSetRvrtTms`/`WSetRvrtRem`) is cosmetic and the software
watchdog is the sole safety mechanism.

But five **secondary** docs still say the hardware timer "WORKS" or is
"FUNCTIONAL," with no caveat, and none of them reference the correct finding:

| File | Line(s) | Current text |
|---|---|---|
| `docs/FRANKLINWH_SUNSPEC_QUIRKS.md` | 258, 293, 313 | *"✅ WORKS with proper sequencing! ... Timer=60s accepted, countdown active"*; *"Hardware reversion timer available: WSetRvrtTms works"* |
| `docs/FRANKLINWH_MODBUS_GUIDE.md` | 301 | *"P1: WSetRvrtTms \| ✅ WORKS (60s accepted, countdown active) \| Re-tested 2026-03-13"* — directly contradicts the same file's own lines 55, 134, 252 ("never activates"/"broken") |
| `docs/AGATE_FULL_MODEL_MAP.md` | 53, 64 | *"WSetRvrtTms: NOW FUNCTIONAL (Confirmed 2026-05-14). Countdown active."* |
| `docs/SUNSPEC_DER_SEQUENCER_REFERENCE.md` | 44, 88-89 | *"Phase 2 (reversion safety — WSetRvrtTms) is now FULLY FUNCTIONAL ... VERIFIED (2026-05-14)"* |
| `docs/SUNSPEC_GLOSSARY.md` | 40 | *"Dead-man Timer: Reversion timeout. Confirmed functional 2026-05-14."* |

**Root cause** (via `git log -L` on `FRANKLINWH_SUNSPEC_QUIRKS.md`): the
"WORKS" wording was added 2026-03-13 (`d9ea482`); the correct "Hardware
Reversion is Cosmetic" section was added as a *separate* section the very
next day, 2026-03-14 (`d862c16`) — but the earlier claims were never edited to
match. The May-2026 docs then independently reintroduced the same "confirmed
functional" language after a re-baseline test, apparently conflating "the
countdown register decrements" with "the setpoint actually reverts" — the
same mistake, twice. `docs/backlog.md` has no tracked item to reconcile this.

**Proposed fix:** in all five locations, replace "WORKS"/"FUNCTIONAL"/
"Confirmed functional" with something like *"Countdown decrements correctly,
but does NOT cause reversion — see the Cosmetic/Issue-4 section below. Do not
rely on this for safety."* Add a backlog item to prevent this drift recurring
(a doc-consistency check, or at minimum a cross-link from every mention of
`WSetRvrtTms` to the single canonical finding).

---

## B. Conflicting, unreconciled explanations for registers 16001/16002

Two docs in the same repo attribute these registers to *different* things,
and neither cross-references the other:

- `docs/FRANKLINWH_SUNSPEC_QUIRKS.md:465-466` (2026-03-15): *"16001 \| Self
  Reserve (%) \| 15508 \| Same"* and *"16002 \| TOU Reserve (%) \| 15509 \|
  Same"* — presented as confirmed mirrors, based on a scan that found no other
  non-zero registers in 15900-16100.
- `docs/FRANKLINWH_EXTENSIONS_MANIFEST.md:27-28` (2026-05-15, headed
  "Guess/Notes"): *"16001 \| NPt (Mirror) \| 712 \| NPt \| ... Mirror of
  15508/15509 (Reserves=6)"* and *"16002 \| TmpAmb (Mirror) \| 701 \| TmpAmb \|
  °C \| ... Matches 701.TmpAmb (scaled=20)"* — attributes them to SunSpec
  Model 712's `NPt` and Model 701's ambient temperature instead.

Neither value (16001=8, 16002=20 in a live read this session) is
disambiguated between these explanations. Note also that `16001`/`16002` are
**not present in `constants.py` at all** — only documentation references them;
they're not wired into the library's actual register map or read by any code
path, so this has never been resolved by exercising real code.

**This session found a third data point** neither doc considered: a raw
15000-15039 scan (via `tools/modbus_sunspec2_reader.py --match`) found real
matches for `15020` (→ `713.WHRtg`, battery rated Wh), `15025` (→ `701.LLV`/
`701.LNV`, raw line voltage), and `15036` (→ `713.SoH`, battery state of
health) — none of which appear to be captured in `FRANKLINWH_EXTENSIONS_
MANIFEST.md` yet (that file lists 15012-15035 as bare hex/value dumps with no
description). These should be added.

**Proposed fix:** run the empirical test in section D below to settle 16001/
16002 definitively, then update whichever doc is wrong (or both, if neither
current explanation survives contact with a real test) and add a note
explaining why the other explanation was wrong, so this doesn't drift a third
time. Also add the three newly-confirmed 15000-range matches to
`FRANKLINWH_EXTENSIONS_MANIFEST.md`.

---

## C0. `power_flow` (cmd 1301) — a rich, mostly-undocumented read source for cross-referencing

Live output captured 2026-07-13 (`franklinwh-local -i 192.168.1.100
power_flow`). `catalog.py:92` documents this endpoint only at a high level
("Live power flow: run_status, mode, p_uti/p_sun/p_gen/p_fhp/p_load, soc,
t_amb, daily kWh"), and only `run_status` has an actual decode table
(`RUN_STATUS` dict, `catalog.py:185-196`; confirmed `run_status=2` →
"Discharging", consistent with `p_fhp=1073` being positive). `mode` is
explicitly documented as an arbitrary programme id, not something to decode
further (`catalog.py:181-183`).

**Everything else in this payload is undocumented** and worth using as
correlation ground truth against the still-unknown Modbus registers, because
several fields carry much higher precision than raw Modbus registers ever
would (`soc: 51.684208`, `kwh_sun: 21.334961`, `t_amb: 13.2`) — matching a
*scaled* raw Modbus integer against these (e.g. does some register equal
`t_amb * 10 = 132`, or `soc * 10 ≈ 517`?) is a stronger test than the coarse
whole-number coincidence matching used in section A/B, since it's far less
likely to collide by chance:

- `slaver_stat`, `elecnet_state`, `infi_status` — status/enum fields, no
  decode table anywhere in the repo.
- `cd_alm`, `ent`, `genStat`, `offgridreason` — single-value flags/codes, no
  description. `offgridreason` in particular is worth capturing during an
  actual `set_offgrid()` test (section D) since it should populate with a
  real value at that moment. `cd_alm` — possibly "cabinet door alarm," given
  `CABINET_OPEN` exists as a named alarm bit in both M701.Alrm and
  M714.DCAlrm (Issue 12) — worth checking if it correlates with either.
- `main_sw` (3 elements), `pro_load` (3 elements), `doStatus`/`diStatus` (4
  elements each) — digital I/O / switch-state arrays, no field-by-field
  description. Candidate correlates for the small integer-flag registers in
  the still-unmatched 15000-range list (15021=1, 15026=1, etc.) — worth
  testing whether any of those track a specific element of these arrays.
- `sharp`/`peak`/`flat`/`valley` (7-element TOU billing-period energy
  arrays) and `fhpSn`/`fhpSoc`/`fhpPower` (per-battery-unit arrays, useful on
  multi-unit installs) — likely **not** exposed via Modbus at all; valuable
  in their own right for the bridge's pricing/reporting features regardless
  of the Modbus cross-referencing goal, but out of scope for this proposal.

**Recommendation:** capture `power_flow` alongside every Modbus register dump
in the test plan (step 1 below), not as an afterthought — its higher-precision
fields make several of the "no match found" 15000-range registers worth a
second look with a scaled-value comparison instead of only integer-equality.

---

## C-cloud. `franklinwh-cloud`'s `get_runtime_data` — a fourth, already well-documented ground-truth source

A live `franklinwh-cli raw get_runtime_data` capture this session returned ~50
fields with heavy overlap with `power_flow` plus several new ones (`bms_work`,
`pe_stat`, `sinHTemp`/`sinLTemp`, `soChBat`, `soOutGrid`, `batOutGrid`,
`gridChBat`, `genChBat`, `genVoltage`, `remoteSolarEn`, `report_type`,
relay states). **Before assuming any of these are undocumented, check
`/Users/davidhona/dev/franklinwh-cloud` first** — unlike `franklinwh-local`'s
`power_flow`, most of this is already named, typed, and in some cases decoded
in that repo's code (not just its markdown docs, which don't cover this
endpoint at all — `API_FIELD_REGISTRY.md`'s own header scopes it to
"static/config APIs" only):

- **`bms_work` is a simple offset of `run_status`, not a separate unknown**:
  `const/states.py:54-56` documents `bms_work = run_status + 5, always` (with
  an explicit warning not to look it up against the `RUN_STATUS` table — it
  uses a different dict, `BMS_STATE`, with a different offset). Confirms
  `bms_work=[7]` and `run_status=2` in the same capture are the same fact
  twice, not two facts.
- **`pe_stat`** decodes via a documented `PCS_STATE` table (`const/states.py:38`).
- **`gridChBat`/`soOutGrid`/`soChBat`/`batOutGrid`/`genVoltage`/
  `remoteSolarEn`** are all named, typed, and unit-labeled in `models.py`
  (lines 136-143, 172) and `franklinwh_cloud/cli_commands/schema.py` (lines
  77-83, 125) as part of a "Power Flow"/"Power Measurements" field group.
- **`kwhSolarLoad`/`kwhGridLoad`/`kwhFhpLoad`/`kwhGenLoad`** are documented
  in `docs/AGENT_GROUND_TRUTH.md` §5 as likely cumulative/lifetime Wh (not
  daily kWh) — status explicitly marked "unconfirmed, pending FEM app
  cross-reference" by that repo's own authors. Don't treat these as daily
  totals when cross-referencing.
- **Relay encoding is vendor-specific and counter-intuitive — load-bearing
  for any Modbus `main_sw`/`pro_load`/`doStatus`/`diStatus` correlation
  later**: `docs/AGENT_GROUND_TRUTH.md` §1 states this has been "flipped
  incorrectly multiple times by successive agents" and must not be
  "corrected" based on normal electrical-engineering intuition — it's the
  vendor's own convention, confirmed against live hardware. (That same
  section has an internal wording inconsistency between its header framing
  and its own worked example — not this proposal's repo to fix, just noting
  it exists so it isn't silently relied on without double-checking against
  the worked example, not the header prose.)

**Recommendation:** before writing off any `get_runtime_data` field as
unknown, grep `franklinwh-cloud/franklinwh_cloud/` (`models.py`, `schema.py`,
`const/states.py`, the `mixins/` directory) first — a lot of reverse-engineering
legwork already happened there. The genuinely-still-open question is whether
any of these *cloud* fields correlate with the still-unmatched *Modbus*
15000-range registers (15007, 15021, 15022, 15026, 15027, 15029, 15030,
15033, 15034, 15035) or the disputed 16001/16002 — that cross-repo
correlation is what section D's test plan is for; the cloud side of the data
just isn't a mystery in its own right.

---

## C. Local API (franklinwh-local, TCP/9000) write inventory

For the empirical test in section D, here's what can actually be changed via
the Local API today (`/Users/davidhona/dev/franklinwh-local`):

| Setting | cmd_type (req→resp) | Method | Fields |
|---|---|---|---|
| Operating mode (Backup/Self-Consumption/TOU) | 1727→1728, `opt=3` | `set_mode(mode)` (`client.py:263-274`) | `current_id` |
| Off-grid / reconnect | 1723→1724, `opt=1` | `set_offgrid(on, soc=5)` (`client.py:276-280`) | `offgridSet`, `offgridSoc` |
| Reserved SoC % (both Self-Consumption and TOU together) | 1405→1406, `opt=1` | `set_mode_soc(self_min, self_max, tou_min, tou_max)` (`client.py:282-293`) | `selfMinSoc`, `selfMaxSoc`, `touMinSoc`, `touMaxSoc` |
| Smart circuit | 1409→1410, `opt=1` | documented in `catalog.WRITES` (`catalog.py:153-156`) but **no dedicated client method yet** — callable via generic `client.call(1409, data)` |

**Critical caveat for the test plan — RESOLVED 2026-07-13 with exact field
names, not just a warning.** There are **three separate, unrelated mode
identifiers** in this ecosystem, confirmed via live `franklinwh-cli` calls
against the cloud API:

- **`oldIndex`** — the legacy numbering, confirmed via `get_mode_info`
  (`{'id': 29287, 'oldIndex': 3, 'name': 'Time-of-Use', 'workMode': 1, ...}`):
  **1=Backup, 2=Self-Consumption, 3=TOU**. This is what Modbus register
  15507 follows (matches `catalog.py:205-213`'s "register 15507/oldIndex"
  wording exactly — it's not a separate scheme, it's literally this field).
- **`workMode`** — the newer/canonical numbering, confirmed via
  `get_all_mode_soc` (`workMode: 1→'Time-of-Use'`, `workMode: 2→
  'Self-Consumption'`, `workMode: 3→'Emergency Backup'`): **1=TOU,
  2=Self-Consumption, 3=Backup** — i.e. `oldIndex` and `workMode` swap
  positions 1 and 3 (Backup↔TOU) while agreeing on position 2
  (Self-Consumption). This is the actual "swap" `catalog.py` warned about,
  now pinned to two named, confirmable fields instead of an unlabeled
  comment.
- **`id`** — an arbitrary, per-installation programme identifier (e.g.
  `29287` for TOU, `85232` for Self-Consumption in a live `power_flow`
  capture this session) — **not a 1-2-3 scheme at all**, don't try to map it
  numerically to either of the above. This is what `power_flow`'s `mode`
  field returns (see section C0), and what `franklinwh-local`'s
  `_resolve_mode_id()` resolves a mode name to before writing.

When correlating a Local-API mode change against Modbus 15507 during the
test plan: map by **name** ("Self-Consumption" etc.), or by `oldIndex` if you
need a number, never by `workMode` or `id` — using either of those against
15507 will look like a mismatch that isn't really there.

---

## D. Proposed empirical test plan

Goal: turn "16001/16002 might mirror X" from two competing guesses into a
confirmed answer, and get real correlation data for the still-fully-unknown
15000-range registers (15007, 15021, 15022, 15026, 15027, 15029, 15030,
15033, 15034, 15035 — no match found against the 328-point SunSpec database
or any named extension register so far).

1. **Baseline capture**: read the full 15000-15039, 15500-15513, and
   16000-16002 ranges via `tools/modbus_sunspec2_reader.py --raw ... --match`
   (or the bridge's SunSpec Explorer export), alongside the standard SunSpec
   registers for M701/M713 (ambient temp, line voltage, reserve %) **and**
   `franklinwh-local power_flow` **and** `franklinwh-cli raw get_runtime_data`
   in the same instant (see sections C0 and C-cloud) — the cloud capture adds
   fields with no Modbus/local-API equivalent at all (`sinHTemp`/`sinLTemp`,
   `pe_stat`, per-source kW splits) worth checking against the still-unmatched
   15000-range registers. Higher-precision fields (`soc`, `t_amb`, `kwh_*`)
   catch things whole-number coincidence would miss. Record timestamp.
2. **Change Self-Consumption reserve only** via `set_mode_soc()`, e.g. set
   `selfMinSoc` to a value distinct from the current TOU reserve (so the two
   are no longer numerically equal — this session's data had them both at 8%,
   which is exactly why 16001 couldn't be disambiguated). Re-capture the same
   ranges immediately after. Whichever of 16001/15508/15509 changes (or
   doesn't) tells you definitively what 16001 tracks.
3. **Change operating mode** via `set_mode()`, watching 15507 and 15016 (a
   second register that numerically matched the current mode value in this
   session's data, `2`, but wasn't confirmed as a real duplicate). Remember
   the Backup/TOU swap caveat above when interpreting 15507.
4. **Toggle off-grid** via `set_offgrid()` and scan the full 15500-16002
   range for anything that changes — no register in this range is currently
   documented as tracking off-grid state at all, so this is genuinely
   unexplored territory.
5. **Repeat the baseline capture 2-3 times over an hour** without changing
   anything, to distinguish registers that are genuinely static/reserved
   (real duplicates or unused) from ones that fluctuate with normal
   operation (like the Home Load cluster — 15011, 15013, 15506, 16000 all
   track something in the Home Load ballpark but diverged by up to 52W
   between two single-snapshot captures taken minutes apart this session;
   only repeated, timestamped sampling can tell whether they're the same
   signal at different precision/latency or genuinely different quantities).
6. Update `FRANKLINWH_EXTENSIONS_MANIFEST.md` and the 16001/16002 conflict
   (section B) with whatever the data actually shows, cross-linking both
   docs so they can't drift apart again the way WSetRvrtTms's docs did.

## Recommended owner / sequencing

This proposal spans two read-only-here repos (`franklinwh-modbus` docs,
`franklinwh-local` is only a reference for the test plan, not something that
needs changing) — hand to whichever session is dedicated to
`franklinwh-modbus` per the established multi-session workflow. Suggested
order: fix section A first (safety-relevant, no new testing needed, just
doc correction) — then run section D's empirical test — then fix section B
with real data.
