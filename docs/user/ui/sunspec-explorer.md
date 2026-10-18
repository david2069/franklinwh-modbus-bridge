# SunSpec Explorer

The SunSpec Explorer (`?tab=explorer`) lists every SunSpec model and register the aGate
exposes, with live values. It is a tool for checking a value, reporting a bug, or
learning the aGate's registers. It only reads, apart from PICS marks that it stores in the
bridge's database.

!!! info "Off by default, and needs a real aGate"
    - On a new install the Explorer is switched off. Turn it on in
      [Settings → Admin → Feature Modules](settings-admin.md#feature-modules).
    - It needs a connected, real aGate. With only a mock or demo gateway it shows
      **Needs a connected aGate**, because a mock has no Modbus registers.

![SunSpec Explorer with only the demo gateway](../../images/ui/page-sunspec-explorer.png)

## Layout

**Connection bar** (top): the gateway's host, port and unit ID, how many models and points
were found, and:

- **↻** resets the panel sizes.
- **Refresh Catalog** scans the aGate again for its SunSpec models. Use it after a firmware
  update. If no catalog exists yet the page offers this button in the middle.

**Model tree** (left):

- **Filter models/points…** searches model names and point names.
- **Hide unpolled models (no live values)** hides models the bridge does not poll.
- Each model shows its ID, name and number of points. Click one to open it.

**Points detail** (right), for the chosen model:

| Control | What it does |
|---|---|
| **Addr: SunSpec / Base-0** | Shows register addresses the SunSpec way (1-based, as in the SunSpec documents) or 0-based (as many Modbus tools expect). |
| **Access** | All, R (read-only) or RW (writable). |
| **PICS** | Filters by compliance mark (see below). |
| **↻** | Reads the model from the aGate now. |

If the model is not polled, a notice says **Not actively polled — no live values** with a
**⚡ Read Now** button.

Points table columns: Point, Label, Value (hover a coded value to see what each code
means), Type, Unit, Addr, Access, PICS.

### PICS marks

PICS is a compliance checklist. Click the PICS badge on a row to cycle it through:

| Mark | Meaning |
|---|---|
| U | Unimplemented (default when there is no value) |
| S | Supported (default when the point has a live value) |
| T | Tested |
| F | Failed |
| X | Not applicable |

Your marks are saved in the bridge's database, per model and point.

## Exporting models

**Export…**, at the top of the page next to **Refresh Catalog**, opens a window to export the
SunSpec map with its current values, for a GitHub issue, a Gist, or someone writing another
integration.

| Option | Choices |
|---|---|
| Models | **All models** (the default), **Displayed models** (the ones the list on the left shows after its filter and *Hide unpolled models*), or **Choose models** (click model numbers to add or remove them). |
| Format | **JSON** (with the gateway's model and firmware) or **CSV** (one row per point). |
| **Include FranklinWH extensions** | On by default. Adds the FranklinWH extension registers (15000–15039, 15500+, 16000+) as their own section: in JSON a `franklinwh_extensions` block whose `note` says they are **not SunSpec** (undocumented by FranklinWH, named by this bridge, some unconfirmed); in CSV, rows with Model `ext`. The file name gains `_ext`. |
| **Redact serial number** | On by default. Replaces the serial number with `<serial>`: in model 1's `SN` and anywhere else it appears, including its last 8 characters. Turn it off only if you want to share the serial. |

The preview shows exactly what you will get. The gateway's IP address and name are never
included. Then:

- **Copy** copies it.
- **Download** saves it, named for example `sunspec_all_redacted.json`.
- **Save as Gist** copies it and opens GitHub's new Gist page. Paste it, give the file the
  name shown under the preview, then choose:
    - **Create secret gist**: unlisted, so only people with the link can find it. It is not
      private: anyone with the link can read it.
    - **Create public gist**: listed on your profile and searchable.

  The bridge sends nothing to GitHub itself, and needs no GitHub account or token.

The same export is available from the REST API: `GET /api/models/export` (see the
[API reference](../../dev/api-reference.md)).

!!! warning
    Read the preview before you share it. You are responsible for what you post.

## FranklinWH Extension Registers

The bottom panel lists the FranklinWH vendor registers (15000+, 15500+, 16000+) that are
outside the SunSpec models. Click its header to show or hide it.

- **Search registers…** filters by name or description.
- **All / Identified / Unknown** shows every register, only the ones with a name (from
  FranklinWH documentation, or from site notes marked *unconfirmed*), or only the unnamed
  ones (`Reg15xxx`, description *Unknown*).
- **Hide zeros** hides registers that read 0.
- To export them, use **Export…** at the top with *Include FranklinWH extensions* on.

Columns: Addr, Register, Description, Value, Hex, Type, Acc (access), Unit.

!!! warning
    Extension registers are undocumented by FranklinWH and can change with firmware.

Panel sizes are draggable, and the layout is remembered in this browser.
