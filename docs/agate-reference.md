# FranklinWH aGate Real Device Reference

Captured 2026-05-21 from a live aGate X unit.
Source library: `franklinwh-modbus` (`~/dev/modbus`).
SunSpec spec: SunSpec Alliance Modbus Specification V1.2 PDF.

## Device Identity

| Field        | Value                             |
|--------------|-----------------------------------|
| Manufacturer | FranklinWH Technologies Co., Ltd  |
| Model        | aGate X                           |
| Firmware     | V10R01B04D00                      |
| Serial       | 10060006A02F00000001              |
| Unit ID      | 1                                 |
| IP           | 192.168.1.100                     |
| Port         | 502 (Modbus TCP)                  |
| Base Address | 40000                             |
| AC Wiring    | Single-Phase (230V Nominal)       |

## SunSpec Models (17 total)

| Model | Name                | Addr  | Len | Key Points                                                    |
|-------|---------------------|-------|-----|---------------------------------------------------------------|
| 1     | common              | 2     | 66  | Mn, Md, Vr, SN, DA                                           |
| 502   | solar_module        | 1096  | 28  | OutPw, OutV, OutA, OutWh, InW, InV, InA, Tmp                 |
| 701   | DERMeasureAC        | 70    | 153 | W, VA, Var, PF, A, LLV, LNV, Hz, TotWhInj/Abs, Temps        |
| 702   | DERCapacity         | 225   | 50  | WMaxRtg, VAMaxRtg, WChaRteMaxRtg, WDisChaRteMaxRtg, VNomRtg |
| 703   | DEREnterService     | 277   | 17  | ES, ESVHi, ESVLo, ESHzHi, ESHzLo, ESDlyTms, ESRmpTms        |
| 704   | DERCtlAC            | 296   | 65  | WSetEna, WSet, WSetPct, VarSetEna, WMaxLimPct, PFWInjEna     |
| 705   | DERVoltVar          | 363   | 67  | Ena, NPt=4, NCrv=3, curve-based                              |
| 706   | DERVoltWatt         | 432   | 31  | Ena, NPt=2, NCrv=2, curve-based                              |
| 707   | DERTripLV           | 465   | 105 | Ena, NPt=5, NCrvSet=2, trip curve                            |
| 708   | DERTripHV           | 572   | 105 | Ena, NPt=5, NCrvSet=2, trip curve                            |
| 709   | DERTripLF           | 679   | 135 | Ena, NPt=5, NCrvSet=2, trip curve                            |
| 710   | DERTripHF           | 816   | 135 | Ena, NPt=5, NCrvSet=2, trip curve                            |
| 711   | DERFreqDroop        | 953   | 32  | Ena, NCtl=2, droop settings                                  |
| 712   | DERWattVar          | 987   | 44  | Ena=0, NPt=6, NCrv=2, curve-based                            |
| 713   | DERStorageCapacity  | 1033  | 7   | WHRtg=13600, SoC, SoH, WHAvail, Sta                          |
| 714   | DERMeasureDC        | 1042  | 43  | DCA, DCW, DCWhInj, DCWhAbs                                   |
| 715   | DERCtl              | 1087  | 7   | LocRemCtl, DERHb, ControllerHb, AlarmReset, OpCtl            |

## Key Polling Points (v1 bridge)

These are the points most relevant for HA entity publishing:

### Power Flow (Model 701 DERMeasureAC)
- `W` — Active power (W), scale factor W_SF=0
- `VA` — Apparent power (VA)
- `Var` — Reactive power (Var)
- `PF` — Power factor, SF=-3
- `A` — Total AC current (A), SF=-1
- `LLV` / `LNV` — Voltage (V), SF=-1
- `Hz` — Frequency (Hz), SF=-3 (stored as uint32, e.g. 50000 = 50.000 Hz)
- `TotWhInj` — Total energy injected (Wh, uint64)
- `TotWhAbs` — Total energy absorbed (Wh, uint64)
- `TmpAmb` / `TmpCab` / `TmpSw` — Temperatures (C), SF=-1

### Battery (Model 713 DERStorageCapacity)
- `WHRtg` — Energy rating: 13600 Wh (13.6 kWh)
- `WHAvail` — Energy available (Wh)
- `SoC` — State of charge (%), SF=-1 (raw 660 = 66.0%)
- `SoH` — State of health (%), SF=-1 (raw 955 = 95.5%)
- `Sta` — Status enum

### DC / Battery Power (Model 714 DERMeasureDC)
- `DCW` — DC power (W)
- `DCA` — DC current (A)
- `DCWhInj` — DC energy injected (Wh, uint64)
- `DCWhAbs` — DC energy absorbed (Wh, uint64)

### Solar (Model 502 solar_module)
- `OutPw` — Output power (W)
- `OutWh` — Output energy (Wh, acc32): 13042294 Wh observed
- `OutV` / `OutA` — Output voltage/current

### Capacity Ratings (Model 702 DERCapacity)
- `WChaRteMaxRtg` — Charge rate max: 5000 W
- `WDisChaRteMaxRtg` — Discharge rate max: 5000 W
- `VAMaxRtg` — Apparent power max: 5800 VA
- `VNomRtg` — Nominal voltage: 240 V
- `AMaxRtg` — Current max: 24.5 A

### Control (Model 704 DERCtlAC)
- `WSetEna` — Active power set enable (RW)
- `WSet` — Active power setpoint W (RW, int32)
- `WSetPct` — Active power setpoint % (RW)
- `WMaxLimPct` — Max power limit % (RW)
- `WSetRvrtTms` — Revert timeout (does NOT work on FranklinWH per CLI notes)

### DER Control (Model 715 DERCtl)
- `LocRemCtl` — Local/Remote control mode
- `AlarmReset` — Alarm reset (RW)
- `OpCtl` — Set operation (RW)

## FranklinWH Extension Registers (non-SunSpec)

These are proprietary registers outside the SunSpec model map.

### Documented Extensions (15506-15513)

| Address     | Name              | RW | Type   | Unit | Description                          |
|-------------|-------------------|----|--------|------|--------------------------------------|
| 15506       | HomeLoad          | R  | uint16 | W    | Home load active power               |
| 15507       | OngridMode        | RW | uint16 | —    | Operating mode (0=Backup, 2=Self, …) |
| 15508       | SelfReserve       | RW | uint16 | %    | Self-consumption SOC reserve          |
| 15509       | TOUReserve        | RW | uint16 | %    | TOU SOC reserve                      |
| 15510-15511 | PVEnergyTotal     | R  | uint32 | Wh   | PV energy total (high:low words)     |
| 15512-15513 | PVEnergyProximal  | R  | uint32 | Wh   | PV energy proximal (high:low words)  |

### High-Resolution Extension (16000+)

| Address | Name              | RW | Type   | Unit | Description                 |
|---------|-------------------|----|--------|------|-----------------------------|
| 16000   | HomeLoadHiRes     | R  | uint16 | W    | High-resolution home load   |
| 16001   | (unknown)         | R  | uint16 | —    | Observed value: 6           |
| 16002   | (unknown)         | R  | uint16 | —    | Observed value: 20          |

### Extension Register Writability

Extension registers 15507-15509 require **installer unlock** to be writable.
In read-only mode, the healthcheck reports:
```
extension_readonly: Ongrid Mode, Self Reserve, Tou Reserve
```

### Raw Register Observations (15000-15043)

Non-zero values observed in the 15000-15043 range (purpose not fully documented):

| Address | Raw(hex) | uint16 | Notes                        |
|---------|----------|--------|------------------------------|
| 15007   | 010C     | 268    |                              |
| 15011   | 028C     | 652    |                              |
| 15013   | 028B     | 651    |                              |
| 15014   | FFFF     | 65535  | Likely sentinel / not-set    |
| 15015   | FFE8     | 65512  | int16=-24                    |
| 15016   | 0002     | 2      |                              |
| 15017   | 0014     | 20     |                              |
| 15018   | FFFF     | 65535  | Likely sentinel / not-set    |
| 15019   | FFFF     | 65535  | Likely sentinel / not-set    |
| 15020   | 3520     | 13600  | Matches WHRtg (battery kWh)  |
| 15021   | 0001     | 1      |                              |
| 15024   | C350     | 50000  | Possibly Hz*1000             |
| 15025   | 097E     | 2430   | Matches voltage raw values   |
| 15026   | 0001     | 1      |                              |
| 15029   | 0046     | 70     |                              |
| 15030   | B58A     | 46474  |                              |
| 15033   | 0017     | 23     |                              |
| 15034   | 217D     | 8573   |                              |
| 15035   | 0298     | 664    |                              |
| 15036   | 03BB     | 955    | Matches SoH raw (95.5%)      |
| 15040   | FFF7     | 65527  | int16=-9                     |
| 15043   | 0001     | 1      |                              |

## Operating Modes (Register 15507)

| Value | Mode              |
|-------|-------------------|
| 0     | Backup            |
| 2     | Self-Consumption  |
| (TBD) | TOU              |

## Status Output Example

```
Solar:       0W Idle          Battery: Discharging 600W
Home:      624W Consuming     Grid:    ~0W (Grid Following)
LocRemCtl: Local           Available: 9.0/13.6 kWh
Control:   Local (aGate Native: Self-Consumption)
```

## Healthcheck Output Summary

Checks performed:
- Connection OK
- Models 704, 713, 701 present and readable
- WSetEna/WSetMode/WSet state (zombie detection)
- SoC within safe range
- Grid voltage, frequency, connection state
- AC type, inverter state, operating mode
- Extension register writability test
- Alarm state

## Scale Factor Reference

| Model | SF Field     | Value | Applies to                                     |
|-------|-------------|-------|-------------------------------------------------|
| 701   | A_SF        | -1    | Current (raw/10)                                |
| 701   | V_SF        | -1    | Voltage (raw/10)                                |
| 701   | Hz_SF       | -3    | Frequency (raw/1000)                            |
| 701   | W_SF        | 0     | Active power (raw*1)                            |
| 701   | PF_SF       | -3    | Power factor (raw/1000)                         |
| 701   | VA_SF       | 0     | Apparent power                                  |
| 701   | Var_SF      | 0     | Reactive power                                  |
| 701   | TotWh_SF    | 0     | Energy (Wh)                                     |
| 701   | Tmp_SF      | -1    | Temperature (raw/10)                            |
| 702   | W_SF        | 0     | Active power capacity                           |
| 702   | PF_SF       | -3    | Power factor                                    |
| 702   | VA_SF       | 0     | Apparent power                                  |
| 702   | V_SF        | 0     | Voltage                                         |
| 702   | A_SF        | -1    | Current (raw/10)                                |
| 713   | WH_SF       | 0     | Energy (Wh)                                     |
| 713   | Pct_SF      | -1    | Percent (raw/10)                                |
| 714   | DCA_SF      | 0     | DC current                                      |
| 714   | DCW_SF      | 0     | DC power                                        |
| 714   | DCWH_SF     | 0     | DC energy                                       |

## CLI Reference (franklinwh_cli.py)

Connection: `-i <IP> -u <unit_id> -p <port> -t <timeout> -b <base_address>`

Key commands:
- `--status` — Compact power flow summary
- `--healthcheck` — Full device health report
- `--monitor` — Rich TUI dashboard (5s refresh, live power flow)
- `--mode <name>` — Switch hardware operating mode (register 15507)
- `--charge <W>` / `--discharge <W>` — Direct power control
- `--max-charge` / `--max-discharge` — Use M702 nameplate rating
- `--standby` — Set 0W
- `--stop` — Release control back to cloud
- `--sequence <json>` — Multi-step register read/write
- `--revert <secs>` — Software auto-revert timer (hardware WSetRvrtTms broken on FranklinWH)
