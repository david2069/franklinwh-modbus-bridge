# Modbus/SunSpec Speculation Handoff — DER-control write-plane & the SPAN/ControllerHb theory

> **Audience:** the franklinwh-modbus Agent.
> **Status:** SPECULATION / research notes for review. **No code.** Nothing here is
> validated on hardware beyond the read-only observations explicitly marked "observed".
> **Author context:** produced from the franklinwh-local session (sendMqtt protocol work);
> cross-repo writes into franklinwh-modbus are out of scope for that agent, hence this
> handoff. Owner to decide what (if anything) to act on.
> **Gateway under discussion:** aGate at 192.168.1.100:502, unit 1, single-phase 230V AU.

---

## 1. The question
Why do Modbus writes to the FranklinWH **extension registers** (OnGridMode / reserve SoC)
and to **SunSpec DER controls** (Model 704 setpoints, Model 715 OpCtl) get *accepted*
(FC06 success echo) but appear to **do nothing** — and can SPAN's role be emulated to
unlock them?

## 2. Observed facts (read-only; not speculation)
- **Model 715 DERCtl** live on this gateway:
  - `LocRemCtl` = **1 (Local Control)** — type enum16, access **R** (read-only), addr 41090.
  - `DERHb` (DER Heartbeat) = 0 — uint32, **R**, addr 41091.
  - `ControllerHb` (Controller Heartbeat) = 0 — uint32, **RW**, addr 41093.
  - `AlarmReset` = 0 — uint16, **RW**, addr 41095.
  - `OpCtl` (Set Operation) = **0 (Stop the DER)** — enum16, **RW**, addr 41096.
- **Model 704 DER AC Controls**: `WSet`/`WSetPct` (active power) etc. are **RW** — active
  power control ("Command Power") is known to work on this unit.
- **Extension registers** (base 15000/15500/16000):
  - `15507 OnGridMode` = 2 (Self-Consumption), **RW**, uint16 (SPAN PICS types it enum16:
    1=BACKUP_ONLY, 2=SELF_CONSUMPTION, 3=TOU).
  - `15508 SelfReserve` = 15 %, **RW**, uint16.
  - `15509 TouReserve` — **RW** (per franklinwh-modbus; may mirror 15508 on some units).
  - `16000 HomeLoadHiRes` = 615 W, **R**.
- **SunSpec Modbus interface is ON** (from sendMqtt cmdType 1205 `der_comms`):
  `sunsMdEn=1`, served at 192.168.1.100:502. IEEE 2030.5/SEP2 side is OFF
  (`enable=0`, `status2030_5=0`, lfdi/sfdi empty).
- **SPAN Lab PICS notes** (2023-08-03, SPAN unit 10060001A00F22510306, fw V10R04B11D90):
  - `OnGridMode`: *"validated: changing in FranklinWH reflects via Modbus. not yet
    validated: Modbus write is reflected in FranklinWH app."*
  - `SelfReserve`: *"Resets to 10 when OnGridMode is updated."*
  - `TouReserve`: *"Resets to 50 when OnGridMode is updated."*

## 3. Core speculation — the write-plane is gated by the SunSpec DER control handshake, not a flag
`LocRemCtl=1 (Local Control)` and is **read-only**, so it cannot be flipped directly. In the
SunSpec DER model the DER chooses Local vs Remote itself, based on a **controller heartbeat
watchdog**:
1. A controller writes an **incrementing** value to `ControllerHb` (41093) continuously.
2. The DER, once it sees a live+authorized controller heartbeat, echoes/advances `DERHb`
   (41091) and transitions `LocRemCtl` → **Remote**.
3. In Remote, the DER honors `OpCtl` (41096) and the Model 704 power setpoints.
4. If the heartbeat stops, the DER **fail-safes back to Local** (safety).

Current state `ControllerHb=0` (no heartbeat) ⇒ DER stays Local ⇒ writes accepted but
ignored. **This is the most likely "lock."** SPAN's role is literally to be that controller
keeping `ControllerHb` alive — consistent with `ControllerHb` appearing in the SPAN PICS.

**Speculative extension:** *if* holding a valid ControllerHb flips `LocRemCtl` to Remote,
it may unlock not just Model 704/OpCtl but also the FranklinWH **extension** writes
(`15507/15508/15509`) — OR those extensions may be gated separately (SPAN/Lumin
commissioning "authorization"). Two independent gates are possible:
- Gate A: heartbeat/watchdog → Local↔Remote (standard SunSpec).
- Gate B: an "accept remote controller / commissioned" authorization the aGate only grants
  a commissioned SPAN — which would explain PICS "Modbus write reflected in app: not yet
  validated" even *with* SPAN present.

## 3a. IEEE 2030.5 / SEP2 / CSIP — the OTHER remote-control path (currently OFF)
The same DER-comms config screen (sendMqtt cmdType **1205 der_comms**) carries a full
**IEEE 2030.5 (Smart Energy Profile 2.0 / SEP2)** client config, presently **disabled and
unregistered**:
- Observed on 1205: `enable=0`, `status2030_5=0`, `uri=""`, `lfdi=""`, `sfdi=""`,
  `dcap2030_5="/sep2/dcap"` (device-capability URI), `pin="111115"` (registration PIN).

Why this matters to the write-plane hunt:
- **2030.5 is an independent remote-control / authorization channel**, parallel to SunSpec
  Modbus. It is a REST/TLS protocol where the DER registers (via LFDI/SFDI + PIN) to a
  utility/DERMS **2030.5 server** and then accepts mode / curtailment / power commands.
- **CSIP** (Common Smart Inverter Profile, the US utility profile) is built on 2030.5, and
  FranklinWH holds a **CSIP certificate** (`~/Downloads/UPDATED_FranklinWH_CSIP_Certificate_
  CS-000040.pdf`). So 2030.5 is a *first-class, certified* control surface on this hardware,
  not a vestige.
- **Strong Gate-B candidate:** the "commissioned controller authorization" that Modbus
  writes seem to require may be granted through a *registered controller* relationship —
  and 2030.5 registration is exactly such a relationship. Enabling + registering 2030.5
  (LFDI/SFDI enrolled, `status2030_5` → active) might (a) itself place the DER under remote
  authority, and/or (b) reveal how the aGate models "authorized remote controller" vs. an
  arbitrary Modbus master holding only a heartbeat.

Speculative interactions to consider (documentation only):
- Does turning `enable`→1 and registering 2030.5 change `LocRemCtl` (715) or make the
  Modbus extension writes (`15507/15508/15509`) persist? i.e. is authorization shared
  across the 2030.5 and Modbus control planes, or per-plane?
- Could 2030.5 and Modbus **compete** for control (last-writer / priority)? SunSpec 715
  `LocRemCtl` is a single mode — two remote controllers may conflict.
- The unconfirmed sendMqtt cmdTypes **1207** (`{enable,mode}`, placeholder-ish 111/222) and
  **1209** (`{enable:1}`) sit in the grid/DER range next to 1205 — plausibly 2030.5/DERMS
  sub-settings (e.g. a DER-management enable + mode). Worth correlating when 2030.5 is toggled.

> ⚠️ **Real-world caution:** enabling/registering 2030.5 can hand dispatch authority to a
> **utility/DERMS**. Only point it at a controlled/self-hosted 2030.5 server for testing —
> never a live utility endpoint — and understand it may enroll the battery in external control.

## 4. Reserve-SoC is coupled to mode (important cross-finding)
Reserve is **not** an independent setpoint: per PICS, changing `OnGridMode` **resets**
`SelfReserve`→10 and `TouReserve`→50. This is why the FranklinWH **cloud** sets reserve
*inside* the mode-switch call (`updateTouModeV2` carries `soc`). Implication for Modbus:
even if 15508/15509 become writable, a subsequent OnGridMode write will clobber them —
so any controller must write reserve **after** (or atomically with) mode, and re-assert it.
On the sendMqtt-local channel there is **no** reserve write at all (exhaustively confirmed);
reserve is cloud-only there.

## 5. Proposed VALIDATION protocol (for the Modbus Agent to consider — documentation only)
> ⚠️ **SAFETY:** `OpCtl=0` = **"Stop the DER"** (cease/disconnect). Establishing Remote
> control while `OpCtl=0` could disconnect/stop the battery. Any test must set `OpCtl` to
> the connect/"enter service" enum **before** Remote activates, and have a plan to drop the
> heartbeat to fail-safe back to Local. Do not run unattended. Owner authorization required.

Suggested read-only-then-minimal sequence (each step gated on the prior result):
1. **Baseline read:** LocRemCtl, DERHb, ControllerHb, OpCtl, WMaxLimEna/WSet, 15507/15508.
2. **Heartbeat only:** write an incrementing ControllerHb every N seconds (per model's
   `ControllerHb` timeout, if discoverable via Model 715 metadata / DERHb behaviour).
   **Observe** whether `DERHb` starts advancing and whether `LocRemCtl` → Remote. Change
   nothing else. This step alone is low-risk (no dispatch command issued).
3. **If LocRemCtl flips to Remote:** test the *benign* extension write first —
   e.g. read 15508, write the **same** value back, read-back to confirm it now persists
   (vs. the silent-discard seen with no heartbeat). Snapshot + restore.
4. **Only then**, with OpCtl deliberately set to the in-service enum and heartbeat held,
   evaluate whether 15507/15508 writes take effect (and whether 15508 survives a 15507
   write, given the reset-on-mode-change behaviour). Snapshot + restore mode AND reserves.
5. If LocRemCtl does **not** flip with a good heartbeat → Gate B (authorization) is real;
   document that Modbus control requires commissioned-SPAN authorization and stop.

## 6. What would confirm / refute the theory
- **Confirm:** ControllerHb heartbeat alone flips LocRemCtl→Remote and makes a same-value
  15508 write persist on read-back. ⇒ heartbeat is the sole gate; SPAN is emulatable.
- **Refute:** LocRemCtl stays Local despite a spec-valid heartbeat. ⇒ separate authorization
  gate; likely needs commissioned SPAN/Lumin **or a registered 2030.5 controller** (§3a);
  a bare Modbus master won't unlock it.
- **2030.5 discriminator:** if LocRemCtl/extension-writes activate only after a 2030.5
  registration (not from a Modbus heartbeat alone), authorization is registration-based, and
  the "SPAN unlock" is really a *commissioned-controller* relationship — reproducible with a
  self-hosted 2030.5/CSIP server rather than SPAN hardware.

## 7. Open questions for the Modbus Agent
- Does Model 715 expose a `ControllerHb` **timeout** point (period the DER expects)? SunSpec
  715 typically has a heartbeat-timeout register — check the model's point list.
- Is there an authorization/enable point (Model 703 Enter Service, or an extension flag) that
  distinguishes "commissioned controller" from an arbitrary Modbus master?
- Does `sunsMdEn` (sendMqtt 1205) vs a *separate* write-enable differ? (1205 only turns the
  interface on for reads; it is NOT the write-plane unlock — confirmed.)
- Does OpCtl have a documented enum set on this firmware (0=Stop; what is connect/enter-svc)?
- **2030.5:** does enabling/registering 2030.5 (1205 `enable`→1, LFDI/SFDI enrolled,
  `status2030_5`→active) change `LocRemCtl` or make extension writes persist? Is authorization
  shared across the 2030.5 and Modbus control planes, or per-plane? Do 2030.5 and Modbus
  compete for the single `LocRemCtl` mode? Are cmdTypes 1207/1209 the 2030.5/DERMS sub-toggles?
- Where does the CSIP cert (`~/Downloads/UPDATED_FranklinWH_CSIP_Certificate_CS-000040.pdf`)
  fit — does 2030.5 registration require a utility server, or can a self-hosted CSIP server
  enroll the aGate for controlled testing?

## 8. Cross-references
- SPAN PICS spreadsheet: `~/Downloads/PICS_span_20230711_SPANcomments20230803.xlsx`
  (SPAN Lab notes quoted in §2).
- SunSpec models in `~/Downloads/sunspec_model_704.json`, `...713*.json`, `...714.json`,
  and the DER Information Model spec PDFs (V1.0 / V1.2) — 715 DERCtl semantics.
- franklinwh-local `docs/PROTOCOL.md`: reserve-coupled-to-mode note; sendMqtt has no local
  reserve write; 1205 der_comms (sunsMdEn) documentation.
- franklinwh-modbus `tools/modbus_compliance_tester.py`: existing SPAN-write-lock note
  (15507–15509 writes silently discarded when SPAN write-plane locked; read-back mandatory).

---
*Speculation only. Everything in §3–§7 is a hypothesis to be validated by the Modbus Agent
with owner authorization and live-hardware safety controls. No code produced.*
