# Dependencies and troubleshooting

## Dependency matrix

✔ = required for a meaningful value or for the action to work. ◐ = optional, changes the result. Blank = not needed.

### Conditions

| Sensors | Gateway polling | Trusted battery rating | Constants | Utility service | Energy rates | NEM region / HA price entity | Location (lat/lon) | HA instance + exposed entity | Default gateway only |
|---|---|---|---|---|---|---|---|---|---|
| `battery.*` live (soc, power, status, reserves) | ✔ | | | | | | | | |
| `battery.capacity_kwh`, `stored_kwh`, `remaining_kwh` | ✔ (713.WHRtg) | | | | | | | | |
| `battery.time_to_*` (ETA) | ✔ (702 nameplate rate) | | ✔ (defaults exist) | | | | | | |
| `solar.*`, `pv.*`, `grid.*`, `load.*`, `mode.*` | ✔ | | | | | | | | |
| `inverter.*` | ✔ (702 nameplate rate) | | | | | | | | |
| `gateway.*` | ✔ (nameplate) / config | | | | | | | | |
| `const.*` | | | ✔ (defaults 20 / 100 / 30 %) | | | | | | |
| `tariff.*_window_active` | | | | ✔ | | | | | |
| `tariff.season`, `time_period`, `tier`, `buy_rate`, `sell_rate`, `import_billable` | | | | ✔ | ✔ | ◐ (dynamic plan) | | | |
| `tariff.spot_price`, `feed_in_price` | | | | ◐ (dynamic plan) | | ✔ | | ◐ (HA price entity) | |
| `tariff.export_charge_*` | ✔ | | | ✔ (export charge) | | | | | ✔ |
| `demand.*`, `bonus.*` | ✔ | | | ✔ (peak demand / export bonus) | | | | | ✔ |
| `energy.period_import_kwh`, `import_cost`, `export_credit`, `unpriced_*` | ✔ | | | ✔ (billing cycle) | ✔ | ◐ (dynamic plan) | | | ✔ |
| `energy.*.total_kwh`, `energy.*.today/this_week/this_month/ytd_kwh` | ✔ (energy counters) | | | | | | | | |
| `fixed.*` | | | | ✔ (standing charges) | | | | | |
| `service.*` | | | | ✔ (plan terms; defaults are permissive) | | | | | |
| `dispatch.*` | ◐ (`manual` needs the live setpoint) | | | | | | | | |
| `weather.*`, `solar_forecast.*` | | | | | | | ✔ (+ kWp, tilt, azimuth) | | |
| `solar_forecast.day_pct`, `vs_expected_pct` | ✔ (today's solar energy) | | | | | | ✔ | | |
| `time.*` | | | | | | | | | |
| `ha:<instance>:<entity>` | | | | | | | | ✔ | |

Where to set each one:

| Dependency | Where |
|---|---|
| Gateway polling | **Settings → Site & Gateways → Gateways** (host, port, enabled). Poll interval: **Modbus Poller**. |
| Trusted battery rating | **Settings → Site & Gateways →** *your gateway* **→ Battery power rating** (calibrate). |
| Constants | **Settings → Energy & Automation → Automation Constants** |
| Utility service, energy rates, peak demand, export bonus, export charge, standing charges, plan terms, billing cycle, timezone | **Settings → Energy & Automation → Electricity Utility Service(s)** |
| NEM region, HA price entities | **Settings → Site & Gateways → Wholesale spot price** |
| Location and PV array | **Settings → Site & Gateways → Location & Solar** (or *use the Home Assistant location*) |
| HA instance and exposed entities | **Home Assistant** tab |
| Bridge timezone | the confirm/change bar at the top of the page |

### Actions

| Action | Running gateway | Trusted rating (direction) | Extension-register writes accepted | HA instance | `notify.*` service | MQTT |
|---|---|---|---|---|---|---|
| Force Charge | ✔ | ✔ charge | | | | |
| Force Discharge | ✔ | ✔ discharge | | | | |
| Force Standby | ✔ | | | | | |
| Release | ✔ | | | | | |
| Self Reserve %, TOU Reserve % | ✔ | | ✔ (often not) | | | |
| Operating Mode | ✔ | | ✔ (often not) | | | |
| No battery action | | | | | | |
| HA action — Entity | | | | ✔ (+ entity exposed) | | |
| HA action — Notify | | | | ✔ | ✔ | |
| Failure alerts | | | | ✔ | ✔ (a Notify action on the entry) | |

The scheduler does not use MQTT. MQTT is only how the bridge's own entities reach Home Assistant; schedules run without a broker.

## Troubleshooting

### A condition is always false

| Cause | How to tell | Fix |
|---|---|---|
| The sensor has no value | **Test Verification** shows the row amber, *✕ no value* | Configure what the sensor needs (matrix above). |
| Typed `true` / `false` for a yes/no sensor | the row fails although the sensor shows true | Compare with `1` or `0`. |
| A Lookup sensor has no value | the row fails; the Lookup sensor reads — in the picker | Configure that sensor, or use a Value. |
| Wrong unit | `tariff.spot_price` is c/kWh, `tariff.buy_rate` is $/kWh; HA prices keep their own unit | Check the `live:` value and unit next to the row. |
| `in numbers` used on text | text never matches a number list | Use **matchlist** for names (TOU, Self-Consumption). |
| Per-mode reserve | `battery.reserve_self_pct` / `reserve_tou_pct` have a value only while that mode is active | Use `battery.reserve_pct`. |
| Forecast or price is stale | weather/forecast older than its day, NEM price older than 30 min | Check internet access from the bridge and the settings card's status. |
| HA entity `unavailable` | amber in Test Verification | Fix it in Home Assistant; until then the rule does not act (by design). |
| Second gateway | demand, bonus and billing figures measure the default gateway only | Gate on per-gateway sensors (power, energy totals) instead. |

### A condition is always true when it should not be

| Cause | Fix |
|---|---|
| Tariff window sensors read `False` (not "no value") when no service is configured, so `== 0` is always true | Configure the service, or add a second condition that needs it. |
| Calculators (`demand.*`, `bonus.*`, `fixed.*`, `energy.import_cost`) read `0` when not configured | As above. |
| `service.*` permissions default to allowed | Set the plan terms on the service. |
| An empty `not in` list is true | Fill the list. |

### An action is refused or does nothing

| Symptom | Cause | Fix |
|---|---|---|
| Control log: *Refused Force Charge/Discharge: … rating unknown / not confirmed* | The battery rating is not trusted in that direction | Calibrate the rating. Until then the Activity Log may show the entry as *ok* followed by *expired* on the next tick; the control log has the refusal. |
| Control log: *Released: battery ran at … W for a … W force …* | The overshoot guard fired and the rating is now contradicted | Recalibrate. |
| Activity Log: *deferred — target under manual control* | A manual force command is active and the conflict policy is defer/wait | Release the manual command, or set the entry to **override**. |
| Activity Log: *skipped — gateway held by …* | A higher-priority (or older) entry holds the gateway | Change priorities, or narrow one of the windows. |
| Reserve or mode "ok" but nothing changed | The aGate acknowledges extension-register writes but ignores them | Use force commands; see [Actions](actions.md#reserve-and-operating-mode). |
| *not_effective* after 90 s | Setpoint accepted, battery not moving (another limit, the gateway's own logic, or a full/empty battery) | Check SoC, reserve and the aGate; the bridge re-sends once. |
| Force runs at less than full power with Power empty | A power staged earlier (Controls tab, HA *Command Power*) carries over | Set Power on the entry. |
| Force stops early with *expired* | Target SoC reached, a time limit (maybe carried over from a manual command), an external release or the overshoot guard | Check **Logs** for the release reason. |
| HA action *failed — no HA configured* or *not found* | No HA instance, or it was removed | Add it on the Home Assistant tab. |
| No failure notification arrived | The entry has no Notify action | Add a Notify action to the entry. |

### An entry never fires

| Cause | How to tell | Fix |
|---|---|---|
| Disabled | grey dot, *disabled* badge; the list shows *Enabled only* by default | Enable it. Imported, duplicated and template entries start disabled. |
| Gate never passes | *gated* rows in the Activity Log with the failing values | Fix the conditions; use Test Verification. |
| Hold-for too long | *waiting* row but no dispatch | Shorten the hold-for, or check the gate is not flickering. |
| Another entry wins | *skipped* rows | Priority. |
| Weekly trigger with no days | *Next fire* shows — | Tick at least one day. |
| One-off date has passed | *Once (passed)* | Change the date. |
| Duration (min) 0 on a force entry | it fires and releases about a minute later | Set a duration. |
| Always entry already ran once | nothing new in the log after its exit or Stop | Restart the bridge, or use a cron trigger instead (see [Triggers](triggers.md#always-evaluate)). |
| Wrong clock | the time in the top bar, `time.tz_ok` = 0, *Next fire* at the wrong hour | Confirm or change the timezone. |
| Gateway not running | no rows at all for that target; the gateway is offline in Settings | Start or fix the gateway. Site/Service targets only include running gateways. |
| Window was during an outage | *missed* row | Choose *late-fire even if window passed* if you want it to run late. |
