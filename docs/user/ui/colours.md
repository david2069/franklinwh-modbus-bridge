# Colours

The bridge uses colour to show state, and each colour means the same thing on every page:
the top bar, the Dashboard and the [simple dashboard](user-dashboard.md).

| Colour | Means | Examples |
|---|---|---|
| **Red** | **Critical.** Act now. | Battery SoC at or below 5%. *Disconnected*: the browser has lost the bridge. A gateway that can't be reached. Errors. |
| **Amber** | **Warning.** Close to critical, or something to notice. | Battery SoC at or below the active reserve. Grid not connected (the aGate is islanded). An active battery command (the **Release** button). Stale data. |
| **Orange** | Energy being drawn. | Battery discharging. Grid importing. |
| **Green** | Active. | Solar generating, home load, battery charging, grid exporting, grid connected. SoC above the active reserve. |
| **Grey** | Inactive. | Solar not generating, no load, battery on standby, no grid flow. |

Red is kept for critical, so a red item always needs attention. Importing from the grid
is normal, so it is orange, not red.

## Battery state of charge

| SoC | Colour |
|---|---|
| At or below 5% | Red (critical) |
| At or below the active reserve | Amber |
| Above the active reserve | Green |

The active reserve is the aGate's own (`ext.15508`, shown on the Dashboard's Battery card).
If the aGate doesn't report one, only the 5% level applies.

## Not covered by the rule

- **Chart lines** (Grid, Solar, Battery) have a fixed colour each, so you can tell the series
  apart. That colour isn't a state.
- **Buttons and selected tabs** use the page's accent colours.
