# FranklinWH aGate X — Modbus Point Issues

Catalog of missing, broken, cosmetic, blocked, and unverified/guessed Modbus
points on the FranklinWH aGate X — for vendor reporting and library upstream
tracking.

Device: aGate X, Firmware V10R01B04D00, tested 2026-03-13.
Serial: 10060006A02F00000001. Connection: 192.168.1.100:502 unit 1.

Documented in `franklinwh-modbus` library at
`~/dev/modbus/src/franklinwh_modbus/types.py` (PICS_STATUS dict,
line 200+). SunSpec spec PDF at
`~/Downloads/Secure-SunSpec-Modbus-Specification_final.pdf`.

**FranklinWH's official SunSpec PICS certificate (SM-000028)** — the ground
truth used to confirm Issue 12's findings — is checked into this repo at
`docs/reference/UPDATED_FranklinWH_Modbus_PICS_SM-000028.xlsx` (and a `.tsv`
export alongside it), copied from Downloads on 2026-07-13 since Downloads
isn't durable storage; it is also published by SunSpec at
<https://sunspec.org/wp-content/uploads/2024/04/Franklin-WH-Certificate-SM000028.pdf>.
Cross-referenced against the official SunSpec model
definitions in the vendored `sunspec2` Python package
(`venv/lib/python3.14/site-packages/sunspec2/models/json/model_701.json` /
`model_713.json` / `model_714.json`), which encode the actual bit/enum
orderings the PICS table's rows follow.

**Some findings below predate that certificate** and cover ground it does not:
a full conformance table for **Model 502 (`solar_module`)**, which is absent from
the official certificate, and register-by-register notes on the 15500+ extension
block — several raising questions that were never answered. Where those findings
conflict with later third-party test logs, they are the stronger evidence; see
Issues 8 and 11.

---

> **SunSpec certification:** FranklinWH's conformance is published as document
> `SM-000028` — [Modbus PICS](https://sunspec.org/wp-content/uploads/2009/03/UPDATED_FranklinWH_Modbus_PICS_SM-000028.xlsx) and [IEEE 1547 certificate](https://sunspec.org/wp-content/uploads/2009/03/UPDATED_FranklinWH_Modbus_1547_Certificate_SM-000028.pdf);
> vendor listing at <https://sunspec.org/contributing-members/franklin-wh/>.
> The PICS is the
> authority for which models and points a firmware is certified for, and is the
> reference these issues are written against. The 15500+ extension registers are
> NOT SunSpec and fall outside it.

## 1. SAFETY-CRITICAL: WSetRvrtTms (M704, Register 327)

**Status:** Cosmetic — countdown timer counts down but **reversion never fires**.

The SunSpec M704 auto-revert mechanism (WSetRvrtTms / WSetRvrtRem / WSetEnaRvrt / WSetRvrt) is designed to automatically release power setpoints after a timeout. On the aGate X, the countdown register works mechanically — WSetRvrtRem (329) ticks down — but when it reaches zero, **no reversion occurs**. WSetEna stays active, WSetPct stays at the commanded value.

This means there is **no hardware safety net** if a controlling application crashes mid-command. The bridge implements a software watchdog (1-hour default) as the only protection.

**Vendor ask:** Implement WSetRvrtTms reversion per SunSpec M704 spec. When the countdown expires, WSetEna should be reset to 0 and power should revert to WSetRvrt target.

**Related registers:**
| Register | Point | Status |
|----------|-------|--------|
| 322 | WSetRvrt | Cosmetic — target value is sticky but never applied |
| 326 | WSetEnaRvrt | Cosmetic — writable but reversion never fires |
| 327 | WSetRvrtTms | Cosmetic — countdown works, no physical reversion |
| 329 | WSetRvrtRem | Cosmetic — readback of countdown, cosmetic only |

---

## 2. BLOCKED: Writes silently discarded (0/160 conformance tests pass)

These registers accept writes without error but have **zero physical effect**:

| Register | Model | Point | Impact |
|----------|-------|-------|--------|
| 310 | M704 | WMaxLimPctEna | Cannot enable power curtailment |
| 311 | M704 | WMaxLimPct | Cannot set curtailment limit |
| 331 | M704 | VarSetEna | Cannot enable reactive power control |
| 251 | M702 | WMax | Cannot set max power ceiling |
| 1092 | M715 | ControllerHb | Safety heartbeat silently discarded |
| — | M715 | LocRemCtl | Stuck on "Local" (value 1); never reports "Remote" even while a VPP setpoint (704.WSetEna=1) is actively controlling the battery |

**Vendor ask:** Either implement these per SunSpec spec, or return Modbus exception code on write attempts so integrators know they're unsupported.

**LocRemCtl (M715)** is non-functional for control-ownership detection: confirmed
2026-06 that it reads "Local" even under an active VPP dispatch. Integrators
**cannot use it to know who controls the battery** — must infer from
`704.WSetEna` + behaviour (does battery power track the commanded setpoint).
Relevant to the bridge scheduler's control-state detection (see
`docs/scheduling-and-orchestration-design.md` §4a).

**ControllerHb (M715.1092) is especially concerning** — this is the SunSpec safety heartbeat mechanism. If an integration relies on the heartbeat as a dead-man switch and it's silently ignored, the battery could remain in an unsafe state indefinitely.

---

## 3. MISSING: M714.DCA (Battery DC Current) — Always 0

M714.DCA (DC current) always returns 0 on the aGate X. M714.DCV (DC voltage) point does not exist. This means:
- Battery current cannot be reported
- P/V fallback calculation (current = power / voltage) is impossible
- The library attempts fallback at controller.py:474-480 but it can never succeed

M714.DCW (battery DC power) works correctly. The entity `battery_current_a` exists in the bridge but always shows 0A.

**Vendor ask:** Populate M714.DCA with actual battery DC current, or expose DCV so current can be derived from power/voltage.

---

## 4. CORRECTED: M713.Sta is a health enum (OK/WARNING/ERROR), not battery activity state

**CORRECTED 2026-07-13 — see Issue 12.** This entry originally assumed M713.Sta
was meant to report battery *activity* state (Charging/Discharging/Idle/etc.).
Checked against the official SunSpec Model 713 spec: `Sta` is actually a
3-value **health** enum — `OK` (0), `WARNING` (1), `ERROR` (2) — not an
activity-state field at all. "Always returns 0" therefore means "always
reports OK," which is the *expected* value for a healthy battery, not a
vendor defect. There is no SunSpec-defined register for battery
charge/discharge/idle activity state in Model 713 to ask the vendor to
populate — the library's existing workaround (below) is the correct approach
regardless of this correction, and was already right for the right reasons.

**Design (not a workaround):** Library derives battery activity state from
M714.DCW power direction with ±50W deadband (controller.py:464-471). This is
the correct way to get charge/discharge/idle state — there is no SunSpec
Model 713 point that provides it directly.

**Separate, still-open issue:** the *bridge's* `alarms.py` module has its own
`_M713_STA_NAMES` table that maps Sta's raw value against an 8-value guessed
activity enum (Idle/Charging/Discharging/Holding/Full/Empty/FAULT/Sleep) —
this models a different, incorrect concept than what Sta actually is (see
Issue 12c). It should be replaced with the real 3-value OK/WARNING/ERROR
enum, or dropped from alarm tracking entirely if health status alone isn't
alarm-worthy.

---

## 5. NO INPUT VALIDATION: WSet accepts out-of-range values (PICS Issue 5)

Writing WSet=15000 (150% of WMaxRtg=10000) is silently accepted. No alarm is raised, no clamping occurs. The device attempts to execute the full 15kW setpoint.

**Workaround:** Library implements software power clamping at controller.py:1271-1292 using M702 nameplate ratings (RATED_MAX_CHARGE_W / RATED_MAX_DISCHARGE_W, default 5000W).

**Vendor ask:** Implement input validation at the Modbus layer. Reject values exceeding WMaxRtg or at minimum clamp to the rated value and set an alarm bit.

---

## 6. COSMETIC: PFWInjEna (M704.298, Issue 6)

Power factor injection enable is writable but gates nothing. Writing 1 does not enable power factor control.

---

## 7. DEFECT: TOU Reserve (15509) mirrors Self-Consumption Reserve (15508)

Writing to register 15509 (TOU Reserve) always mirrors the value at 15508 (Self-Consumption Reserve). They cannot be set independently. The library documents this in `set_tou_reserve()` and in `docs/FRANKLINWH_SUNSPEC_QUIRKS.md` ("SOC Reserve Registers — Known Defect").

**The shared value is the active mode's reserve (2026-10-09, two sites).** On the reference aGate in Self-Consumption, the app showed SC 5 % / TOU 15 % / Backup 100 % while 15508 = 15509 = 16001 = 5. A second site's controlled test (franklinwh-modbus#18) saw 15508/15509/16001 follow each mode switch (TOU 20 → EB 100 → SC 25). So Modbus can read the reserve **in force**, and a per-mode reserve only while that mode is active.

**Bridge handling (#34):** `active_reserve_pct` (HA "Active Reserve SOC", 15508 cross-checked against 16001) is always published. `self_reserve_pct` / `tou_reserve_pct` carry a value only while their own mode is active; otherwise they are unknown (MQTT `None`), not a retained wrong number. The scheduler's `battery.reserve_pct` uses the active reserve.

**Vendor ask:** Allow independent reserve levels for Self-Consumption and TOU modes.

---

## 8. EXTENSION REGISTERS (15500+) — Non-standard, require SPAN unlock

These proprietary registers are **not part of any SunSpec model** and require the installer to enable "SPAN Modbus" in the aGate settings:

| Register | Name | R/W | Precision | Notes |
|----------|------|-----|-----------|-------|
| 15500 | EXT_BASE / PVUse | R | — | Base address / PV Installed flag |
| 15501 | apBoxPVUse | R | — | Remote PV Installed flag |
| 15502 | PV Total | R | 1W | Total solar power — **flagged as rounded to nearest 100W and lagging vs. the standard SunSpec `502.OutPw` (register 1119/absolute 41119), asking FranklinWH "is this expected? Can you explain this behavior?"** — apparently never answered |
| 15503 | PV Proximal | R | 1W | Local PV array power — validated; the same lag/rounding note applies |
| 15504 | PV Remote 1 | R | 1W | Remote PV source 1 — validated; same note as above |
| 15505 | PV Remote 2 | R | 1W | Remote PV source 2 — **not validated** at certification time |
| 15506 | Home Load | R | ~100W | Quantized in ~100W steps |
| 15507 | OnGrid Mode | R/W | — | 1=Backup, 2=Self-Consumption, 3=TOU — this is the `oldIndex` numbering (confirmed via cloud API `get_mode_info`). **Do not confuse with `workMode`** (1=TOU, 2=Self, 3=Backup — swaps Backup/TOU vs. `oldIndex`) or the arbitrary per-installation `id` (e.g. 29287, 85232) used elsewhere — three different, unrelated numbering schemes for the same concept. See `docs/modbus-library-docs-update-proposal.md` |
| 15508 | Self Reserve % | R/W | 1% | Self-consumption reserve SOC — **see Issue 11: this resets to a default (10%) when `OnGridMode` is updated**, a more specific characterization than the later "SPAN-locked" tests |
| 15509 | TOU Reserve % | R/W | 1% | TOU reserve SOC — same reset-on-mode-change behaviour, default 50% |
| 15510-15511 | PV Output Wh (Total) | R | 1Wh | 32-bit unsigned, lifetime PV energy — this register was **explicitly requested during conformance review** (not present at cert time), added by FranklinWH some time after |
| 15512-15513 | **proximalOutputWh** (PV Energy Proximal) | R | 1Wh | 32-bit unsigned — corrected 2026-07-13, this is NOT unknown; confirmed via both the bridge's own SunSpec Explorer catalog and `franklinwh-modbus`'s `SUNSPEC_MODEL_REFERENCE.md:171`. **Also confirmed by the original 2023 request**: *"proximalOutputWh - (= register 1117)"* — register 1117/absolute 41117 is `502.OutWh` (SunSpec Model 502 solar_module lifetime output energy), i.e. this extension register was explicitly designed to mirror that standard SunSpec value, not an independent measurement. In one live sample this exactly equalled Total (15510) — plausible if there's no remote PV array contributing |
| 16000 | Home Load Hi-Res | R | ~1W | Undocumented by FranklinWH, discovered 2026-03-15 by the library author |
| 16001 | **Disputed** | R | — | Two conflicting explanations exist in `franklinwh-modbus`'s own docs (mirror of Self/TOU Reserve % vs. Model 712.NPt) — never reconciled, not wired into any code. See `docs/modbus-library-docs-update-proposal.md` |
| 16002 | **Disputed** | R | — | Same as above (mirror of TOU Reserve % vs. Model 701.TmpAmb) |

**Additionally, a separate 15000-15039 block** exists (distinct from the "extension" 15500+ range above) that's a bare hex/value dump in `franklinwh-modbus`'s own `FRANKLINWH_EXTENSIONS_MANIFEST.md` with no description for most entries. Cross-referencing a live raw read against the full 328-point SunSpec value database (via `tools/modbus_sunspec2_reader.py --match`) found three confirmed matches not yet in that manifest: **15020 → `713.WHRtg`** (battery rated Wh), **15025 → `701.LLV`/`701.LNV`** (raw line voltage), **15036 → `713.SoH`** (battery state of health, raw). **15025 is now triple-confirmed**: `franklinwh-cloud`'s `schema --live` output independently shows `grid_line_voltage` (raw API key literally `gridLineVol÷10`) = 242.50V from a raw value of 2425 — the exact same number, with the exact same ÷10 scale factor SunSpec's `V_SF=-1` implies. Three independent sources (Modbus raw register, SunSpec model spec, cloud API) agree exactly. The rest of that block (15007, 15021, 15022, 15026, 15027, 15029, 15030, 15033, 15034, 15035) remains genuinely unmatched against any known SunSpec value.

**Writability of this block, and 16000-16002, is unresolved** — everything
above came from reads only. A same-value write-back probe
(2026-07-15, `docs/reference/register-writability-probe.md`,
`tools/probe_register_writability.py`) found the device ACKs a write to
*every* register in both ranges with no Modbus exception, including
addresses that are almost certainly read-only telemetry mirrors (e.g.
15020) — meaning this firmware doesn't gate writes by address at the
protocol level, and a same-value probe therefore *can't* distinguish
genuinely writable registers from ones that silently discard the write.
Determining real writability would need the same differential-value +
read-back methodology already used to establish 15508/15509's blocked
status (Issue 11) — not yet attempted here, and higher-risk for registers
whose function is completely unknown.

**Vendor ask:** Document these registers officially. The 15510-15511 (PVOutputWh) 32-bit counter is critical for energy tracking but not documented. Register 16000 provides vastly better home load precision than 15506 but is completely undocumented, and 16001/16002 are disputed even within FranklinWH-adjacent tooling.

**Library gap:** PVOutputWh (15510) is NOT read by `_read_extension_solar()` even though the register batch (15500-15513) already fetches it — `regs[10]` and `regs[11]` exist but are not mapped. Should be added to the library return dict as a 32-bit value: `(regs[10] << 16) | regs[11]`. Currently the bridge reads it via a separate raw pymodbus connection (poller.py:131-147).

**A previously-uncatalogued SunSpec model exists**: **Model 502 (`solar_module`)**, registers 1096-1125 (absolute 41096-41125), not present in the official certificate (SM-000028) but fully conformance-tested — includes `OutWh` (1117), `OutPw` (1119, both referenced above as the "real" registers the 15500-range PV extensions mirror), and a named alarm bitfield (`Evt`, 1104) with `GROUND_FAULT`, `INPUT_OVER_VOLTAGE`, `DC_DISCONNECT`, `MANUAL_SHUTDOWN`, `OVER_TEMPERATURE`, `BLOWN_FUSE`, `UNDER_TEMPERATURE`, `MEMORY_LOSS`, `ARC_DETECTION`, `THEFT_DETECTION`, `OUTPUT_OVER_CURRENT`, `OUTPUT_OVER_VOLTAGE`, `OUTPUT_UNDER_VOLTAGE`, `TEST_FAILED` (plus 6 reserved bits) — all marked "supported." **The bridge does not currently read Model 502 at all** (only M701.Alrm and M714.PrtAlrms are polled for alarms per `controller.py:1753-1758`) — this is a whole alarm surface not yet wired up, separate from the M701/M714 issues in Issue 12.

**See [modbus-library-docs-update-proposal.md](modbus-library-docs-update-proposal.md)** for a full write-up of the 16001/16002 conflict, the 15000-range findings, an empirical test plan using the Local API to resolve them, and a related but separate finding: several `franklinwh-modbus` docs still claim `WSetRvrtTms` (Issue 1) "works," contradicting the library's own current, correct docs/code.

---

## 9. BRIDGE CODE BYPASSING LIBRARY (to be upstreamed)

Two places in the bridge use raw pymodbus instead of the franklinwh-modbus library API:

### 9a. PVOutputWh read (poller.py:131-147)
- Reads register 15510-15511 (32-bit) via raw `ModbusTcpClient`
- **Library fix:** Add `pv_output_wh` to `_read_extension_solar()` return dict using already-fetched regs[10:12]

### 9b. Reserve SOC writes — bridge code hygiene fixed; hardware capability is NOT confirmed working (see Issue 11)
- Was writing registers 15508/15509 via raw `ModbusTcpClient`
- Now uses library's `set_self_consumption_reserve()` and `set_tou_reserve()`, which add a
  protocol-level write-back check (write, then re-read the same register, compare)
- **This only confirms the register accepted the value — it does not confirm the device's
  control logic uses it.** See Issue 11: on every real-hardware test run recorded in the
  `franklinwh-modbus` repo, this write has been observed to **fail** (register reverts /
  never changes), never observed to succeed. Previously mislabeled "FIXED" here — corrected
  2026-07-13 after the bridge author flagged that this still doesn't work in practice.

---

## 10. UNVERIFIED: Alarm/mode bitfields decoded by guesswork, not vendor docs

**Update 2026-07-13 — see Issue 12.** Vendor documentation for most of this was
subsequently located (FranklinWH's SunSpec PICS certificate). M701.Alrm, M714's
alarm register, M713.Sta, and M701.DERMode ("PV Clipped") are no longer merely
"unverified" — they're now **confirmed** to be either mismapped or read from
the wrong register entirely, detailed in Issue 12. What's still genuinely
undocumented (no vendor source found for it) is narrower: the meaning of
`VendorBit8`-`VendorBit31` on M714.PrtAlrms/DCAlrm and `VendorBit16`-`VendorBit31`
on M701.Alrm (bits beyond what either spec defines), and the real value range
of M713.Sta if it ever produces something other than 0/1/2 (e.g. the `State400`
observed in real data — genuinely anomalous under either the old or corrected
model). The rest of this section is kept for historical context on how the
guesses were originally reasoned about; read Issue 12 for what's now confirmed.

The bridge decodes several bitfields/enums using **locally reverse-engineered
tables with no SunSpec or FranklinWH documentation backing them**. SunSpec's
own spec leaves these fields generic (e.g. M714.PrtAlrms is just "bit is 1 if
port has an active alarm — bit 0 is first port", no per-bit names). All names
below were guessed/empirically inferred by the `franklinwh-modbus` library
author, not confirmed by FranklinWH. Source: `franklinwh_bridge/store/alarms.py`.

**M714.PrtAlrms (register 41044, DC port alarms) — bits 0-7 named, 8-31 unknown:**
| Bit | Assumed name | Confidence |
|-----|-------------|------------|
| 0-7 | PortOverVoltage, PortUnderVoltage, PortOverCurrent, PortOverTemp, PortUnderTemp, ContactorFault, FuseFault, PortGroundFault | Guessed, unconfirmed |
| 8-31 | `VendorBit8`...`VendorBit31` — no meaning known at all | Unknown |

**M701.Alrm (register 40076, system alarms) — bits 0-15 named, 16-31 unknown:**
Bits 0-15 (GroundFault, InputOverCurrent, DCOverVoltage, ACDisconnect,
DCDisconnect, GridDisconnect, CabinetOpen, ManualShutdown, OverTemp,
OverFrequency, UnderFrequency, ACOverVoltage, ACUnderVoltage, StringFault,
ArcFault, ThermalDerate) are guessed by name/position analogy to standard
SunSpec DER alarm bit orderings, **not confirmed**. Bits 16-31 unknown
(`VendorBit16`...`VendorBit31`). In 30 days of real device data, every M701
alarm event that fired was a `VendorBit*` (18, 19, 20, 21, 24, 25) — **none
of the 16 "named" bits have ever been observed active**, so even those
names are unverified against real hardware behavior so far.

**Evidence these might not just be undocumented but actively unreliable:**
Over a 30-day sample, every M714/M701 alarm episode self-clears within
10-18 seconds (1-2 poll cycles) and several combine physically contradictory
conditions (over-voltage *and* under-voltage simultaneously; e.g. raw value
891292262 setting `PortUnderVoltage, PortOverCurrent, ContactorFault,
FuseFault` all at once, cleared 10s later). This pattern — instant self-clear,
physically incoherent combinations — looks more like transient register
read noise than real hardware faults, independent of whether the bit names
themselves are correct.

**M713.Sta (register 41039, battery state enum):** values 0-7 are guessed
(Idle, Charging, Discharging, Holding, Full, Empty, FAULT, Sleep). A real
device produced `State400` (value 400) in 30 days of data — **far outside
the assumed 0-7 range** — meaning either the enum has values well beyond
what's guessed, or 400 is itself a glitch/noise read. Unconfirmed either way.

**M701.DERMode bit 2 ("PV Clipped"):** also a guessed name (`# Bit 2 = PV
Clipped` comment in the modbus library, not a vendor doc), and real-world
correlation is poor — of 13 occurrences over 30 days, 12 happened when
`solar_w` was 0-100W (night/dawn/dusk/midnight), when genuine PV output
clipping is physically impossible. Only 1 coincided with actual solar
production (600W, midday). Either the bit is mislabeled (may be a generic
curtailment/limit-mode flag unrelated to sunlight) or it's the same class
of transient glitch as the alarm bits above.

**Vendor ask:** Publish the official bit/enum mapping for M701.Alrm,
M714.PrtAlrms, M713.Sta, and M701.DERMode (or confirm/deny the guessed
names above). Without this, none of the alarm/mode data surfaced in the
bridge's UI (including the Events history table) should be treated as
authoritative — it should be labeled "derived, unconfirmed" wherever shown.

---

## 11. BLOCKED: Reserve SoC writes (15508/15509) fail on all tested real hardware — SPAN lock, unlock mechanism unknown

**Status:** The bridge implements the write path (registers 15508 Self-Consumption Reserve,
15509 TOU Reserve) in the hope it becomes usable once unlocked — it does **not** currently
work against real hardware, on either the Modbus TCP path or FranklinWH's Local API.

**Modbus TCP path — direct hardware evidence of failure:**
- `franklinwh-modbus/tests/results/2026-05-15_live_sequencer_roadmap_results.md:29-37`,
  a live run against a real aGate X (firmware V10R01B04D00):
  ```
  Writing 15508 (Model 15508) = 20 [Raw: 20, Addr: 15508]
  ✗ 15508: Update failure. Current: 6, Expected: 20
  ```
  with the conclusion: *"SPAN Modbus Lock: Step 3 confirms that while Mode switching
  (15507) is unlocked, the SoC Reserve (15508) remains read-only on this system."*
- Independently corroborated in `2026-03-08_cleanup_phase.md:123-146`,
  `2026-05-15_recovery_verification.md:38`, `2026-05-14_ongrid_mode_correction.md:32`, and
  `docs/FRANKLINWH_SUNSPEC_QUIRKS.md:227-233` ("Write Access Table": 15507-15509 = Read ✅,
  Write ❌ Read-Only), and tracked as `docs/backlog.md:50` item `DEF-HW-EXT-READONLY`:
  *"Extension registers 15507–15509 ... are read-only via Modbus ... No workaround
  possible without provisioning."*
- **No test result in the `franklinwh-modbus` repo has ever recorded a successful,
  persisting write to 15508/15509.** Register 15507 (OnGrid Mode) writes *do* succeed —
  only the reserve percentage registers are blocked.

**UPDATE 2026-09-04 — the Local API path is no longer merely "unconfirmed": the
bridge author reports first-hand that setting reserve SoC over it FAILS, and
that the CLOUD path is the one that works.** Specifically:

- The Local API (TCP/9000) is confirmed *capable* for a useful set of controls
  the Modbus path lacks — **switching operating modes, smart circuits,
  generator status, and V2L mode**. But for **TOU / Self-Consumption reserve
  SoC it only returns values: every attempt to set it has failed**, despite
  `set_mode_soc()` existing (cmd 1405 opt=1, writing all four fields
  `selfMin/selfMax/touMin/touMax` together — i.e. the "set them all at once"
  hypothesis below was tried and is not sufficient).
- **The `franklinwh-hybrid` bridge therefore routes reserve-SoC setting to the
  FranklinWH Cloud API** (`update_soc(soc, workMode, electricityType)`) rather
  than the local transport. Its cross-transport table
  (`~/dev/franklinwh-hybrid/docs/CROSS_REFERENCE.md:149`) lists
  local, cloud and REST equivalents; the author's report is what identifies
  cloud as the one used in practice for this capability.

**So the ordering is: Modbus blocked → Local API blocked → Cloud works.** An
earlier draft of this note claimed all three were blocked; that was wrong.

**The cloud path setting reserve SoC is confirmed by the bridge author**
(2026-09-04) — it is not merely implemented, it works. This is the only
transport that can, so any bridge feature exposing a writable reserve SoC must
route there.

What is still missing is only the **recorded artefact**, not the capability: no
set-then-readback capture lives in this repo. The cloud client's own live test
asserts the HTTP call succeeded and re-reads `workMode` alone, never the SoC
value (see the 2026-07-15 update below). Two ready harnesses exist for closing
that gap — `tools/test_cloud_soc_persistence.py` (unrun), and the **FWHAI REST
API** (`https://localhost:8099`, HTTPS not HTTP, auth required), which the
author notes can exercise the same cloud call. Worth capturing once, since it
would turn the single most consequential control on this device from
"known to work" into "evidenced".

**Local API (franklinwh-local, TCP/9000) — the pre-2026-09-04 assessment, kept
for how the conclusion was reached:**
- `franklinwh-local/README.md:7-9` / `docs/index.md:9-11`: repo-wide status is
  *"alpha ... live transport against real hardware should be validated on your own LAN."*
- `set_mode_soc()` (`client.py:283-291`, cmd 1405) exists, but unlike `mode --set`'s CLI
  path (which re-reads `current_id` to confirm the mode actually changed, `cli.py:255,265`),
  the `--soc` CLI path (`cli.py:255-286`) does **not** re-read the reserve value afterward —
  it only prints the raw protocol ack (`cli.py:286`). So even the Local API's own tooling
  has no verification loop for this specific setpoint, and no test result confirms it
  actually persists.
- `catalog.py:141` separately notes some settings "still bounce to the cloud ('System
  Busy')" on this device family — i.e. even locally-issued setpoint writes are known to
  sometimes have no local effect at all, independent of the Modbus SPAN-lock issue above.

**What "SPAN lock detection" in the bridge/library code actually is:** not a dedicated
status check — there is no separate "is SPAN unlocked" register being read anywhere.
It's an inferential error message attached to the write-back mismatch
(`controller.py:1030,1094`: *"Write failed (Read-Only?)... Ensure 'SPAN Modbus' is
unlocked in installer settings"*), a guess at the cause, not a confirmed unlock procedure.

**UPDATE 2026-07-13 — a more specific failure mode, from conformance-review
notes that predate all the 2026 test-result files above**, documented for both
15508 and 15509:

> *"Resets to 10 when OnGridMode is updated. Is this intended behavior? Shouldn't
> this user setting persist unless the user changes it?"* (15508, default 10%)
> *"Resets to 50 when OnGridMode is updated..."* (15509, default 50%)

This is a **different, more specific characterization** than a blanket permanent
read-only lock: the value was observed being **written and then
reset to a default**, tied specifically to `OnGridMode` (15507) updates — not
"every write to 15508/15509 is silently discarded" full stop. It's plausible the
2026 test logs' "Update failure. Current: 6, Expected: 20" results were caused by
this same reset firing between the write and the verification read-back (e.g. if
the write path itself touches `OnGridMode`'s last-updated state internally), which
would look identical to a hard lock in a simple write-then-immediately-verify test
but is a distinguishable, different root cause. **This was never resolved** — no
FranklinWH response to the question is recorded — so it's
unknown whether this is still current behavior on 2026 firmware or whether
FranklinWH later hardened it into the outright block the newer tests show.

**This is exactly the kind of question the bridge's own Modbus Sequencer feature
was built to answer conclusively** (see `src/franklinwh_bridge/sequences/`,
`api/admin.py:588-707`) — a sequence with `write 15508 → verify` immediately
followed by `write 15507 (no-op, same value) → wait_for 15508 != written_value`
would directly distinguish "hard lock" from "resets on mode-touch" without
ambiguity, using the tool purpose-built for this rather than re-reading old logs.
Nobody has run that specific sequence yet — it would be a genuinely new,
first-party result, not a re-derivation of the existing evidence above.

**Note on the above:** `SunSpecSequencer.execute_writes()` (`franklinwh-modbus`
`src/franklinwh_modbus/sequencer.py:296-308`) silently skips a write when the
target value already matches the current value (no Modbus transaction is sent
at all) — so a literal "no-op, same value" write as described can't actually
be issued through the Sequencer as written. A true mode-touch test needs to
cycle the mode away and back (two real transitions), not a same-value poke.

**UPDATE 2026-07-15 — two independent cross-repo leads pointing at the same
mechanism, neither confirmed by readback yet:**

1. **`franklinwh-local`** (the local-API sibling repo) reportedly found that its
   REST API can set reserve SOC if *all* the exposed reserve-SOC fields (cloud
   and local APIs each expose several) are written together, rather than one
   at a time — consistent with the "resets when OnGridMode is updated"
   note above: a *partial* write may be what triggers the reset, not any write.

2. **`franklinwh-cloud`** (the Cloud API client repo,
   `~/dev/franklinwh-cloud`) has exactly the "set together"
   mechanism that characterisation implies should matter:
   - `ModesMixin.set_mode(requestedOperatingMode, requestedSOC=None, ...)`
     (`franklinwh_cloud/mixins/modes.py:39`) accepts an **optional SOC**
     alongside the mode change, sent as **one combined HTTP request** — a
     single query string with both `oldIndex` (mode) and `soc` against
     `hes-gateway/terminal/tou/updateTouModeV2` (`modes.py:212-270`), not two
     sequential calls. `requestedSOC` is explicitly ignored for Emergency
     Backup mode (`modes.py:138`) — TOU/Self-Consumption only.
   - A fully independent `update_soc(requestedSOC, workMode, electricityType)`
     (`modes.py:451`) posts to a separate endpoint (`updateSocV2`), unrelated
     to mode-switching.
   - **Critically, neither is confirmed to persist.** The only live-hardware
     test touching this, `tests/test_live_mode.py`, calls `set_mode(...,
     requestedSOC=5)`, asserts the HTTP call returned success, then reads back
     only `workMode` — **it never reads back the SOC value.** `update_soc()`
     has zero live tests, only mocked ones asserting the call was *made* with
     the right arguments, not that anything changed on the device
     (`tests/test_force_mixin.py:76,104`). This is the identical evidentiary
     gap as the Modbus-side tests above: request-succeeded ≠ confirmed-to-stick.

Both leads independently point at "set mode + reserve together" as the
mechanism worth testing, but across three transports (Modbus, Local API,
Cloud API) nobody has yet closed the loop with an actual persistence readback.
Two concrete follow-ups:
- A genuinely **atomic** Modbus write across 15507-15509 (function code 16,
  "Write Multiple Registers," one transaction — the registers are
  contiguous) would be a stronger test than the Sequencer's current
  per-register sequential writes (and the same gap exists on the read side —
  `execute_reads()` also issues one function-code-3 request per tag, never
  batched). Filed as
  [franklinwh-modbus#11](https://github.com/david2069/franklinwh-modbus/issues/11)
  (`batch_write`/`batch_read` support) for the permanent Sequencer
  capability — but a standalone diagnostic doesn't need to wait for that:
  `tools/test_atomic_block_write.py` uses pymodbus's `write_registers()`
  directly for a real one-transaction write, and (unlike the Sequencer,
  which silently skips a same-value write) can test "keep the current mode,
  change only the reserve, atomically" — a case the Sequencer literally
  cannot express. Pauses/resumes the bridge's own polling around the test to
  avoid the documented concurrent-access corruption risk. Not yet run.
- A `set_mode(requestedSOC=...)` cloud-API call **with an actual Modbus
  readback of 15508/15509 afterward** — closing the gap the existing cloud
  live test leaves open. Script written:
  `tools/test_cloud_soc_persistence.py`. Not yet run.

**Vendor ask:** Confirm what "SPAN Modbus unlock" actually requires (installer-level
setting? firmware flag? account permission?) and document it, or clarify these registers
are permanently read-only via Modbus regardless of provisioning.

**Bridge/UI ask:** Anywhere the bridge exposes Self/TOU Reserve % as a Modbus-writable
control (dashboard battery control card, scheduler, API docs), label it as unconfirmed/
likely non-functional pending vendor clarification, rather than implying it works.

---

## 12. CONFIRMED: alarm/mode decode bugs, found via FranklinWH's own SunSpec PICS certification (2026-07-13)

Issue 10 (above) was written as "guessed, unverified" because no vendor documentation
had been located. That changed: the user located FranklinWH's actual **SunSpec PICS
(Protocol Implementation Conformance Statement) certificate SM-000028**
(`UPDATED_FranklinWH_Modbus_PICS_SM-000028.xlsx` / `.tsv`, now in
`docs/reference/`), cross-referenced
against the official SunSpec model definitions (vendored `sunspec2` Python package,
`site-packages/sunspec2/models/json/model_701.json` / `model_713.json` /
`model_714.json`). Three of the "guessed" mappings in Issue 10 aren't just
undocumented — they're **confirmed wrong or misapplied to the wrong register**:

### 12a. M701.Alrm — off-by-one bit mapping, 3 fabricated bit names, 2 real bits missing

The official SunSpec Model 701 `Alrm` bitfield (17 named bits, all marked
"supported" in FranklinWH's PICS) is, in bit order:

```
0 GROUND_FAULT      6 MANUAL_SHUTDOWN    12 BLOWN_STRING_FUSE
1 DC_OVER_VOLT      7 OVER_TEMP          13 UNDER_TEMP
2 AC_DISCONNECT     8 OVER_FREQUENCY     14 MEMORY_LOSS
3 DC_DISCONNECT     9 UNDER_FREQUENCY    15 HW_TEST_FAILURE
4 GRID_DISCONNECT  10 AC_OVER_VOLT       16 MANUFACTURER_ALRM
5 CABINET_OPEN     11 AC_UNDER_VOLT
```

The bridge's `_M701_ALRM_BITS` (`store/alarms.py:22-40`) has bit 0 correct
(`GroundFault`), but from bit 1 on it's shifted by one position relative to the
real spec, and contains **three bit names that don't exist anywhere in the
official 17-bit list**: `InputOverCurrent` (bit 1), `ArcFault` (bit 14),
`ThermalDerate` (bit 15). It's also **missing two real bits**: `HW_TEST_FAILURE`
(15) and `MANUFACTURER_ALRM` (16) — currently these would fall through to the
generic `VendorBit15`/`VendorBit16` label even though they're officially named.

**Net effect:** if real hardware bit 1 ever fires (DC_OVER_VOLT), the bridge
currently reports it as "InputOverCurrent" — a name that isn't real. Every named
bit from 1-16 is similarly mislabeled. This is a genuine decode bug, not a
documentation gap, and should be fixed in the `franklinwh-modbus` library (the
source of `_M701_ALRM_BITS`'s bit-position assumptions, per its own docstring).

### 12b. M714 — the bridge reads the wrong alarm register entirely

FranklinWH's PICS confirms `PrtAlrms` (register 41044, what
`controller.py:1757-1758` actually reads into `dc_port_alrm`) has **no per-bit
alarm-type names at all** — the official Model 714 spec describes it as "bitfield
of ports with active alarms; bit is 1 if port has an active alarm; bit 0 is first
port." It's a per-**port** summary flag, not a per-**alarm-type** breakdown.

The actual alarm-*type* bitfield with real names is a **different point**,
`Prt.N.DCAlrm` (register 41085 for port 1), confirmed "supported" in
FranklinWH's PICS with non-contiguous bit positions:

```
0 GROUND_FAULT           7 OVER_TEMP              15 ARC_DETECTION
1 INPUT_OVER_VOLTAGE    12 BLOWN_FUSE              19 RESERVED
3 DC_DISCONNECT         13 UNDER_TEMP              20 TEST_FAILED
5 CABINET_OPEN          14 MEMORY_LOSS             21 INPUT_UNDER_VOLTAGE
6 MANUAL_SHUTDOWN                                  22 INPUT_OVER_CURRENT
```

`controller.py` never reads `Prt.N.DCAlrm` anywhere (confirmed via grep) — the
bridge's `_M714_ALRM_BITS` guessed 8-bit table (`PortOverVoltage`,
`PortUnderVoltage`, etc.) is applied to `PrtAlrms`, a register that was never
meant to carry that information. This plausibly explains a lot of what Issue 10
flagged as "transient noise" (self-clearing in 10-18s, physically contradictory
combinations, huge raw values like 891292262) — decoding a per-port summary
flag as if it were 8+ independent alarm types will produce exactly this kind
of nonsensical pattern on a register that, with `NPrt=1`, should mostly only
ever have bit 0 meaningful.

**Fix:** the library should read `Prt.1.DCAlrm` for alarm-*type* detail (using
the bit map above) and use `PrtAlrms` only for its actual meaning (which port,
if any, has an active alarm — useful only once `NPrt > 1`).

### 12c. M713.Sta — wrong semantic model, not just "always 0"

Covered in Issue 4's correction above: `Sta` is a 3-value `OK`/`WARNING`/`ERROR`
health enum per the official spec, not an 8-value activity-state enum
(Idle/Charging/Discharging/etc.) as the bridge's `_M713_STA_NAMES`
(`store/alarms.py:55-64`) assumes. "Always 0" = "always OK," not a defect.
The bridge's alarm-tracking use of this table should be corrected or dropped.

### 12d. M701.DERMode ("PV Clipped") — confirmed UNIMPLEMENTED by FranklinWH's own certification

This is the strongest finding. FranklinWH's PICS lists `DERMode` (register
40078) — **all three of its states, `GRID_FOLLOWING`, `GRID_FORMING`, and
`PV_CLIPPED`** — as **`unimplemented`**. Not "supported," not partially
supported: explicitly unimplemented, in FranklinWH's own signed conformance
statement. Bit ordering itself matches what the bridge assumes (0/1/2 =
Grid Following/Grid Forming/PV Clipped, confirmed against the official model
spec), so that part of the reverse-engineering was right — but the vendor's
own certification says the whole point doesn't do anything on this hardware.

This fully explains Issue 10's finding that 12 of 13 "PV Clipped" events over
30 days occurred with zero solar output: the bridge is deriving `grid_mode`
from a register FranklinWH has certified as not implemented. Whatever value
comes back should be treated as **meaningless noise, not a real signal** —
not "possibly mislabeled," as Issue 10 hedged, but confirmed non-functional.

**Vendor ask:** Either implement DERMode per the SunSpec spec, or note it more
prominently in the register map that reads should be ignored/undefined.

**Bridge ask:** Reconsider whether `grid_mode`/"PV Clipped" should be surfaced
in the UI at all (including the Events history table) given the vendor has
certified this point as unimplemented — at minimum it needs a much stronger
disclaimer than "derived, unconfirmed."

---

## Summary: Action items by owner

### Vendor (FranklinWH firmware) — things only FranklinWH can fix
1. **SAFETY:** Implement WSetRvrtTms reversion (Issue 1)
2. **SAFETY:** Implement ControllerHb (M715.1092) dead-man switch (Issue 2)
3. **SAFETY:** Add WSet input validation / clamping (Issue 5)
4. Populate M714.DCA (battery current) (Issue 3)
5. Implement or return errors for WMaxLimPctEna/WMaxLimPct/VarSetEna/WMax (Issue 2)
6. Document extension registers (15500+), especially 15510 and 16000 (Issue 8)
7. **Document the SPAN Modbus unlock procedure for registers 15507-15509**
   (Issue 11) — Reserve SoC writes fail on every real-hardware test to date;
   Mode switching (15507) already works, only the reserve percentages are
   blocked, and it's unclear what unlocks them
8. Implement M701.DERMode (Grid Following/Grid Forming/PV Clipped), or
   confirm in the register map that it's permanently unimplemented and
   reads should be ignored (Issue 12d) — FranklinWH's own PICS certification
   marks all three states "unimplemented" on this hardware
9. **Answer the two open questions raised at conformance review** and never
   resolved: why does `15502` (PV Total)
   round to 100W and lag vs. the standard `502.OutPw`, and is the
   reset-to-default behavior on `15508`/`15509` when `OnGridMode` updates
   intended (Issue 8, Issue 11 update)

**Note:** M713.Sta does NOT need a vendor ask — it already correctly reports
its actual 3-value OK/WARNING/ERROR health enum per spec (see Issue 4's
correction). There is no SunSpec field to ask FranklinWH to populate for
battery activity state; the library's DCW-derived workaround is the right
design regardless.

### Library (franklinwh-modbus) — bugs in how we decode FranklinWH's correctly-certified data
1. Add PVOutputWh (15510-15511) to `_read_extension_solar()` return dict
2. **Fix `_M701_ALRM_BITS`'s bit mapping** — currently off-by-one from bit 1
   onward vs the official SunSpec Model 701 spec, with 3 fabricated names
   (`InputOverCurrent`, `ArcFault`, `ThermalDerate`) that don't exist in the
   real 17-bit list, and 2 real bits missing (`HW_TEST_FAILURE`,
   `MANUFACTURER_ALRM`) (Issue 12a)
3. **Read `Prt.N.DCAlrm` (register 41085+) for alarm-type detail** instead of
   decoding `PrtAlrms` (41044) as if its bits were alarm types — `PrtAlrms` is
   a per-port active-alarm summary flag with no per-bit names in the spec;
   the real, PICS-certified alarm-type bitfield is a different, currently
   unread register (Issue 12b)
4. Reserve-write functions (`set_self_consumption_reserve()` /
   `set_tou_reserve()`) are implemented with protocol-level write-back
   checking, but the underlying hardware capability is still blocked
   (Issue 11) — no further library change needed until the vendor unlock is known
5. **Feature request: `batch_write`/`batch_read` support in `SunSpecSequencer`**
   (Issue 11, 2026-07-15 update) — both `execute_writes()` and
   `execute_reads()`/`read_value()` issue one Modbus transaction per
   register/tag (function code 6 per write, function code 3 per read), even
   when a step lists several contiguous addresses — the step syntax implies
   one batched operation but neither direction actually is. 15507-15509
   (mode + both reserves) are contiguous, so a genuine function-code-16
   write (and multi-count function-code-3 read) covering all three in one
   transaction is directly possible and would be a materially stronger test
   of the "set together" hypothesis than the current sequential-write
   approximation. Filed as
   [franklinwh-modbus#11](https://github.com/david2069/franklinwh-modbus/issues/11)
   rather than implemented ad hoc in the bridge, per this project's own rule
   that the library owns all Modbus I/O.

### Bridge (franklinwh-modbus-bridge)
1. ~~Replace raw pymodbus reserve writes with library's `set_self_consumption_reserve()` / `set_tou_reserve()`~~ **DONE** (code hygiene only — switched to library API; the underlying write still fails on hardware, see Issue 11)
2. Replace raw pymodbus PVOutputWh read once library adds it
3. Consider hiding/labeling battery_current_a entity as unavailable
4. **Fix or drop `_M713_STA_NAMES`** in `store/alarms.py` — models the wrong
   concept (8-value activity enum vs the real 3-value OK/WARNING/ERROR
   health enum) (Issue 4 correction / Issue 12c)
5. **Reconsider surfacing `grid_mode`/"PV Clipped" at all** (dashboard chart,
   Events table) given FranklinWH's own certification marks the underlying
   register unimplemented — "derived, unconfirmed" is too weak a disclaimer
   now that this is confirmed non-functional, not just unreliable (Issue 12d)
6. Once the library fixes 12a/12b upstream, re-pull the dependency and
   verify the Events table's M701/M714 alarm names against the corrected maps
7. Label Self/TOU Reserve % controls as unconfirmed/likely non-functional
   per Issue 11, until a working unlock procedure is confirmed
8. **Wire up Model 502 (`solar_module`) alarm bits** — not currently read
   anywhere in the bridge or library; a whole alarm surface (`Evt`, 13 named
   bits, all marked "supported") sitting unused
9. **Run a Sequencer test to distinguish "hard SPAN lock" from "resets when
   OnGridMode is touched"** for 15508/15509 (Issue 11 update) — write
   reserve, verify, then separately re-touch `OnGridMode` and watch whether
   the reserve value changes. Nobody has run this specific test yet; it
   would settle a real ambiguity in the existing evidence, using the tool
   built for exactly this kind of repeatable verification
   (`src/franklinwh_bridge/sequences/`)
10. **Run the `franklinwh-cloud` `set_mode(requestedSOC=...)` call with an
    actual Modbus readback of 15508/15509 afterward** (Issue 11,
    2026-07-15 update) — closes the gap the existing cloud live test leaves
    open (it checks `workMode` only, never reads back SOC). Script:
    `tools/test_cloud_soc_persistence.py`. Not yet run.
11. **Run a genuinely atomic (function-code-16) Modbus block write** across
    mode + both reserves (Issue 11, 2026-07-15 update) — tests "keep the
    current mode, change only the reserve, atomically," which the Sequencer
    can't express (it skips same-value writes). Script:
    `tools/test_atomic_block_write.py`. Not yet run.
12. ~~Probe 15000-15039/16000-16002 for Modbus-protocol writability~~
    **DONE (2026-07-15)** — result: no register in either range rejects a
    write, which turned out to mean the safe same-value technique can't
    classify writability on this firmware (Issue 8 update). See
    `docs/reference/register-writability-probe.md`. A real writability
    determination needs differential-value testing per register, not run.
