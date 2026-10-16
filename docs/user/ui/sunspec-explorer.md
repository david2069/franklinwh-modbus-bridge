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
| **JSON / CSV** | Downloads the model's points. |
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

## FranklinWH Extension Registers

The bottom panel lists the FranklinWH vendor registers (15000+, 15500+, 16000+) that are
outside the SunSpec models. Click its header to show or hide it.

- **Search registers…** filters by name or description.
- **Hide zeros** hides registers that read 0.
- **JSON / CSV** download the list.

Columns: Addr, Register, Description, Value, Hex, Type, Acc (access), Unit.

!!! warning
    Extension registers are undocumented by FranklinWH and can change with firmware.

Panel sizes are draggable, and the layout is remembered in this browser.
