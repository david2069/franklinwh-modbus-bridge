# Undocumented extension register writability probe (2026-07-15)

## Purpose

`docs/vendor-issues.md` Issue 8 catalogs two blocks of undocumented,
proprietary FranklinWH Modbus registers — `15000-15039` and
`16000-16002` — that had only ever been **read**, never written to.
Several values in that block coincidentally look like they could be
small control flags (e.g. `15015`/`15016` = 2, `15021`/`15026` = 1), and
the natural question is: are any of these actually writable, i.e. could
they be undocumented control points rather than telemetry mirrors?

This probe answers a narrower, safer version of that question: **does
the device's Modbus implementation reject writes to these addresses at
the protocol level** (an explicit Modbus exception, e.g. illegal
address/function), the way a well-behaved device would for a genuinely
read-only register?

## Approach

For each register in the target ranges, in sequence, one at a time:

1. Read the register's current value.
2. Write that **exact same value** back to it (function code 6, single
   register).
3. Re-read the register immediately and confirm it's unchanged.
4. Classify: `WRITABLE` if the write was ACKed (no Modbus exception),
   `READ-ONLY` if the device returned an explicit protocol-level
   rejection, `READ-FAILED` if even the initial read errored.

**Why this is safe:** same value in, same value out. If a register
holds a live setting, this never changes it. The only way this probe
could alter device behavior is if some address turns out to be
**command-triggered** — where any write, regardless of value, causes an
action rather than storing a value — which is a real but low-probability
risk for a totally undocumented register. To bound that risk further,
registers were probed one at a time with a 0.3s pause between each, and
the bridge's own polling was paused for the duration (`POST
/api/gateways/{id}/stop` / `/start`) to avoid this firmware's documented
history of `0xFFFF` corruption from concurrent Modbus sessions (see
`poller.py`'s own comments on why it stopped duplicate-reading
`15500-15509`).

**What this does NOT test:** whether writing a genuinely *different*
value would have any functional effect, or whether the write is truly
durable rather than immediately overwritten by the device's own internal
refresh cycle. See Results/Interpretation below — this turned out to be
the load-bearing caveat, not a theoretical one.

## Tooling

`tools/probe_register_writability.py` — standalone script, not part of
either shipped package (bridge or library). Uses `pymodbus` directly
(already a bridge dependency), mirroring the raw-register access pattern
already used in `poller.py`'s `_get_vreg_client()`. Pauses/resumes the
bridge's gateway around the run via its own REST API.

```
python tools/probe_register_writability.py --gateway-ip 192.168.1.100 --unit 1
```

## Results (2026-07-15, aGate X, firmware V10R01B04D00)

All 43 registers across both ranges (`15000-15039`, `16000-16002`)
**accepted the write with no Modbus exception and no anomaly** — every
same-value write ACKed cleanly and the immediate re-read matched.

| Outcome | Count | Registers |
|---|---|---|
| WRITABLE (no protocol rejection) | 43 | all of 15000-15039, 16000-16002 |
| READ-ONLY (explicit protocol rejection) | 0 | — |
| READ-FAILED | 0 | — |
| Anomalies (value changed unexpectedly) | 0 | — |

Raw values observed during this run (context, not the finding itself —
several are live telemetry and will differ on a re-run):

```
15000=0     15001=0     15002=0     15003=2700  15004=0
15005=0     15006=65535 15007=64223 15008=0     15009=0
15010=0     15011=603   15012=65535 15013=65511 15014=65535
15015=2     15016=2     15017=20    15018=65535 15019=65535
15020=13600 15021=1     15022=63398 15023=0     15024=50010
15025=2471  15026=1     15027=0     15028=0     15029=71
15030=16830 15031=0     15032=0     15033=27    15034=49134
15035=178   15036=951   15037=0     15038=0     15039=0
16000=588   16001=15    16002=20
```

## Interpretation — the result is a negative finding, not a green light

The clean "all writable" result initially looks like good news, but it
actually **invalidates the premise of the technique for this device**,
not confirms 43 new control points:

- The 15500-series extension registers already have a *confirmed*
  read-only subset (`15500-15506`, per FranklinWH/SPAN's own PICS
  review) and a confirmed-blocked subset (`15508`/`15509`, per
  `franklinwh-modbus`'s live test logs — see `vendor-issues.md` Issue
  11). None of that evidence came from a Modbus protocol exception —
  it came from writing a **different** value and observing the
  read-back either not change (`15500-15506`) or reset shortly after
  (`15508`/`15509`, per SPAN's "resets when OnGridMode is updated"
  note).
- This probe finding zero protocol-level rejections across 43 addresses
  — including addresses almost certainly read-only telemetry mirrors
  like `15020` (matches `713.WHRtg`, a lifetime energy counter that has
  no business being writable) — strongly suggests this firmware's
  Modbus server **does not implement per-register write protection via
  exception responses at all** in this address range. It ACKs the write
  regardless of whether the address is "supposed to" accept one.
- A same-value write can't distinguish "genuinely stored" from "ACKed
  but silently discarded, and the read-back coincidentally matches
  because nothing was going to change anyway" — for a true telemetry
  mirror, writing back its own already-current value is indistinguishable
  from not writing at all.

**Conclusion: this safe technique cannot classify read/write status for
this device family.** The only way to actually determine writability
here is the higher-risk approach this probe deliberately avoided —
write a genuinely *different*, reversible value to one register at a
time and check whether it sticks on read-back (the same methodology
that already established `15508`/`15509`'s blocked status). That
carries real risk for registers whose function is completely unknown
and has not been run.

## Next steps (not started)

1. For registers with a plausible existing match (`15020`, `15025`,
   `15022`, `15003`, `15036` — all confirmed telemetry mirrors per
   `vendor-issues.md`), a different-value write is low-value to attempt
   (we already know what they represent) and higher-risk than needed —
   skip these for write-testing.
2. For the genuinely unmatched registers (`15007`, `15021`, `15026`,
   and others with no `--match` hit), a targeted different-value probe
   is the only way to learn more — but should be done one register at a
   time, on a value chosen to be obviously reversible and functionally
   inert if it *does* take effect (not attempted here; needs case-by-case
   judgment per register, not a blanket script).
3. The complementary, safer path for *functional* identification (as
   opposed to writability) remains: change a setting through the
   official app/Cloud API and watch which of these registers changes in
   response — the same technique that triple-confirmed `15025` as grid
   line voltage. This doesn't answer "is it writable" but narrows what
   several of the unmatched registers actually represent, which is a
   prerequisite for deciding whether a differential-value write is even
   worth the risk.
