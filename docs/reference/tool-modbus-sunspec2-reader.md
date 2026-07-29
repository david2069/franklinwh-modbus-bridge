# Tool notes: `modbus_sunspec2_reader.py` (value-matching / `--match`)

## What this is

`/Users/davidhona/dev/modbus/tools/modbus_sunspec2_reader.py` — a
read-only SunSpec/raw-register diagnostic CLI in the sibling
`franklinwh-modbus` library repo (part of a flat `tools/` dir of ~30
scripts; listed in that repo's `tools/README.md` under "Diagnostic
Utilities"). **That repo is a read-only reference for this project** — no
edits here, this doc exists purely to capture how the tool works since it
has been load-bearing for several `vendor-issues.md` findings (e.g. the
15020/15025/15036 matches, and the raw dump behind
`register-writability-probe.md`) without ever being documented itself.

Strictly read-only: it connects via `pysunspec2`'s
`SunSpecModbusClientDeviceTCP` and only ever calls `.read()` — no write
path exists in the file at all.

## Invocation used in this investigation

```
python3 tools/modbus_sunspec2_reader.py -i <ip> -u <unit> -t <timeout> \
  --raw <start>:<count> -dvalues --match
```

## Full flag reference (the significant ones)

| Flag | Meaning |
|---|---|
| `-i/--ip` | Device IP (required) |
| `-p/--port` | Modbus port (default 502) |
| `-u/--unit` | Modbus unit ID (default 1) |
| `-b/--base-address` | SunSpec base address (default 40000) |
| `-t/--timeout` | Connection timeout, seconds |
| `-m/--models` | Model spec: single/list/range/mixed |
| `-d/--detail` | `minimal\|basic\|values\|detailed\|full` |
| `--point model.point` | Single-point query |
| `--raw start:count` | Raw register dump |
| `-c/--compact` | Compact model-ID summary |
| `--map` | Model-alias map |
| `--nz` | Suppress zero rows in a raw dump |
| `--match` | Cross-reference raw values against a live model scan (see below) |
| `--json` | JSON export |
| `--multi-thread` | Documented no-op — does nothing |

## How `--match` actually works

Only applies to `--raw` reads. It forces a **fresh full model scan on
every invocation** (no caching/persistence — "Building match database
from N models..." runs every time, even for a narrow raw-register
request) and builds a dict `value -> [list of "model.point (raw|scaled)"
candidates]` from every point in every scanned model, indexed under both
its raw integer and scale-factor-applied value.

**Matching is strict equality only.** The raw register's own value is
looked up directly against this dict — it is **never scaled before the
lookup**. Only the SunSpec model side of the comparison gets scale-factor
treatment. There is no tolerance, no fuzzy matching, and multiple
candidate matches for one value are simply listed together (deduplicated,
truncated to the first 2-3) with **no confidence ranking or
disambiguation** — coincidence and genuine correlation look identical in
the output.

Trivial values (`0, 1, -1, 65535, 32768`, and their string forms) are
excluded from the index entirely — the tool's own developer comment
explains why: *"0 matches 1500 fields. That's useless."* This is a
direct, explicit acknowledgment from the tool's author of exactly the
false-positive risk this whole investigation has had to stay disciplined
about.

## What this means for interpreting `[Matches: ...]` output

**A match is a hypothesis, not a confirmation.** This is consistent with
how `vendor-issues.md` already treats these results — e.g. register 15025
was only escalated from "guess" to "triple-confirmed" after an
*independent* third source (`franklinwh-cloud`'s live API field) agreed
on the exact same number with the exact same scale factor; a single
`--match` hit alone was never treated as proof there. The tool's own
exclusion list for trivial values (and the fact that even a "non-trivial"
value can coincidentally recur across the ~192 unique values held by 328
points) means: **treat any single `--match` hit as a lead to independently
corroborate, not a finding to cite on its own** — especially for small,
common integers like `1` or `2`, which sit right at the edge of what the
tool's own exclusion list already flags as too noisy to be useful.

## Related

- `docs/vendor-issues.md` Issue 8 — the 15000-15039/16000-16002 register
  cross-referencing this tool produced.
- `docs/reference/register-writability-probe.md` — the writability probe
  that used a raw dump from this tool as its motivating context.
