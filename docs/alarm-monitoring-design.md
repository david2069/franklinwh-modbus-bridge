# aGate alarm monitoring: design

Issue #430. Accepted by the owner on 2026-10-31. Severities (§4) wait for the owner's decision in #296.

## 1. Why

The owner asked on 2026-10-31: "should we have a process to monitor alarms and this info point? not sure how we are surfacing or monitoring alarms from Modbus now."

The bridge reads the aGate's alarm registers on every poll, but nothing watches them. An aGate fault is seen only if someone opens the events history.

## 2. What the bridge does today

| Source (Base-0 register) | Read | Where it goes |
|---|---|---|
| M701 `Alrm` (76), bits 0–16 standard, 17–31 vendor | every poll | events history: a row when the value changes (`store/alarms.py`) |
| M701 `DERMode` (78) | every poll | events history |
| M713 `Sta` (1039), OK / Warning / Error | every poll | events history |
| M714 `PrtAlrms` (1044), bits 0–7 | every poll | events history |
| **M701 `MnAlrmInfo` (193)**, manufacturer error text | every poll | **shown only:** in the SunSpec Explorer and the Live Points field `mfr_alarm_info` (hidden by default). Not recorded |

The events history appears in the Dashboard's events list and the Timeline's alarm track. No alarm reaches:

- the **Alerts** system and its HA notifications (`fwh_mb_alert_<n>`);
- **HA entities** (MQTT);
- **scheduler conditions**.

Related: #335, where the events history recorded nothing for two off-grid episodes.

## 3. Phases

Each phase is its own PR and is tested on the gateway emulator. Its `alarms` action sets and clears M701 `Alrm` bits by name, including `MANUFACTURER_ALRM`.

### Phase 1: record `MnAlrmInfo`

- A new event source, `M701_MnAlrmInfo`, written to the events history whenever the text changes, including when it changes to empty. The text is recorded as read: no interpretation, no "unknown".
- The events list and the Timeline show it like the other sources.
- **Tests:** a unit test for change-only recording, and an emulator run, or a unit test if the emulator can't set the text.

### Phase 2: alerts

- When an alarm bit sets, or M713 `Sta` leaves OK, or `MnAlrmInfo` becomes non-empty, the bridge raises an alert. The alert is visible on the Alerts page and sent to HA as a persistent notification. The alert clears when the alarm clears.
- There is one alert per gateway, source and bit (or state), with the name as read, e.g. "aGate (Garage): OverTemp (M701 Alrm bit 7)".
- **Debounce:** an alarm must hold for 2 consecutive polls before it raises an alert, so a one-poll flicker never pages anyone. The event row is still written at once.
- Severity per §4.
- **Tests:** raise, hold and clear on the emulator; a flicker raises nothing; two gateways give separate alerts.

### Phase 3: HA entities, per gateway

| Entity | Type | Value |
|---|---|---|
| aGate alarm | binary_sensor (problem) | on while any alarm or a non-OK `Sta` is active |
| Active alarms | sensor (text) | the active names as read, comma-separated; `none` when there are none |
| Manufacturer alarm info | sensor (text) | `MnAlrmInfo` as read |

These are new entities with new unique_ids. Nothing frozen under D1 changes. They follow the gateway's Publish switch and the publishing groups.

### Phase 4 (optional): scheduler condition

`alarm.active` (true/false) per gateway, so an entry can skip or stop a dispatch while the aGate reports a fault. This needs the owner's go-ahead when Phases 1–3 are done.

## 4. Severity: proposed, for the owner's decision (#296)

The colours follow the owner's rule: red is critical, amber is a warning.

| Severity | Sources |
|---|---|
| **Critical (red)** | M701: GroundFault, DCOverVoltage, OverTemp, BlownStringFuse, MemoryLoss, HwTestFailure. M714: PortGroundFault, ContactorFault, FuseFault, PortOverTemp. M713 `Sta` = Error |
| **Warning (amber)** | M701: ACDisconnect, DCDisconnect, GridDisconnect (grid off, as elsewhere), CabinetOpen, ManualShutdown, Over/UnderFrequency, AC Over/UnderVoltage, UnderTemp, ManufacturerAlarm, vendor bits 17–31. M714: Port Over/UnderVoltage, PortOverCurrent, PortUnderTemp. M713 `Sta` = Warning. `MnAlrmInfo` non-empty |

GridDisconnect overlaps the bridge's off-grid detection. Its alert names both the alarm and the off-grid state, so a grid outage doesn't send two unrelated alerts.

## 5. Out of scope

- Interpreting `MnAlrmInfo` text or vendor bits 17–31: shown as read until FranklinWH documents them publicly.
- Phone pushes with buttons, e.g. Acknowledge. That waits for franklinwh-ha-notify, whose phase 3 covers alert buttons.
- Clearing or resetting aGate alarms (M715 `AlarmReset`). That is a write, and stays out.
