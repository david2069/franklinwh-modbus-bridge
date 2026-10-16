# FranklinWH Modbus Bridge

!!! danger "Unofficial software — please read"
    **franklinwh-modbus-bridge is unofficial and is not endorsed, supported, or affiliated
    with FranklinWH in any way.** FranklinWH and aGate are trademarks of their
    respective owners, used here only to describe what this software talks to.

    It is provided "AS IS", for educational and informational purposes only,
    without warranty of any kind, express or implied, including but not limited to
    warranties of merchantability or fitness for any particular purpose. The
    authors and contributors accept no responsibility or liability for any
    consequences of its use.

    By running it you acknowledge that you:

    - are accessing an interface not intended for your use;
    - assume all risk associated with that, including risk to your hardware,
      your warranty, and your electricity supply;
    - will use it responsibly and respect any rate limits;
    - understand that excessive use may affect service for other users.

    **This software issues WRITE commands to grid-connected battery hardware.**
    Incorrect use can discharge your battery when you need it, import power when
    you did not intend to, or leave the system in an unexpected state.

    **COMPLIANCE.** Keep your system compliant with the settings your official
    FranklinWH app or installer configured. Those settings may exist to protect
    battery and gateway safety, to enforce your local grid profile and any
    import/export limits, or to honour incentive, subsidy or VPP programme
    conditions. Using this software to bypass them — deliberately or inadvertently
    — is entirely at your own risk, and is neither condoned nor encouraged.

    **DO NOT CONTACT FRANKLINWH SUPPORT about this software.** Bugs, defects and
    feature requests belong here, not with the vendor:
    <https://github.com/david2069/franklinwh-modbus-bridge/issues>

    - Full terms: <https://github.com/david2069/franklinwh-modbus-bridge/blob/main/LICENSE>
    - SunSpec conformance (PICS SM-000028): <https://sunspec.org/wp-content/uploads/2009/03/UPDATED_FranklinWH_Modbus_PICS_SM-000028.xlsx>
    - Related project documentation: <https://david2069.github.io/franklinwh-cloud/>

Run your FranklinWH aGate from Home Assistant over your local network.

The bridge talks to the aGate over **Modbus TCP**. It shows live power, battery and grid data on its own dashboards, runs **schedules** (force charge or discharge, reserves and modes, with conditions such as SoC, spot price or weather), and publishes everything to Home Assistant as **MQTT entities**.

![The bridge's dashboard](images/ui/dashboard-full.png)

## Start here

| I want to… | Go to |
|---|---|
| Install it in Home Assistant | [Install (HA add-on)](user/install-addon.md) |
| Run it without Home Assistant | [Install (Docker)](user/install-docker.md) |
| Run it from source, or develop it | [Install (Python)](user/install-python.md) |
| Connect my aGate, or try a demo | [First run & setup wizard](user/setup-wizard.md) |
| Learn every screen and setting | [Dashboards & Settings](user/ui/index.md) |
| Automate charge and discharge | [Scheduler](user/scheduler/index.md) |
| Set up my utility, meter and tariff | [Guide: utility, meter & tariff](user/ui/guide-utility-tariffs.md) |
| Add a solar forecast and weather | [Guide: location & weather](user/ui/guide-location-weather.md) |
| Fix or remove HA entities | [Fixing, moving and removing entities](user/home-assistant-entities.md) |
| Use HA entities in schedules, get notifications on my phone | [Home Assistant setup: instance & notifications](user/ha-setup.md) |
| Run the battery without fighting the FranklinWH app | [Battery dispatch: best practice](user/battery-dispatch-best-practice.md) |
| Call the API, or integrate my own tools | [Developer Guide](dev/index.md) |

## What you need

- A FranklinWH **aGate** with **Modbus TCP enabled**. That's an installer setting; the bridge can't turn it on.
- The aGate's IP address, reachable from the machine that runs the bridge.
- For Home Assistant entities: an **MQTT broker**. The Mosquitto add-on is the usual choice.

!!! note "One Modbus client per aGate"
    The aGate's Modbus port serves one client at a time. Don't run two bridges, or
    the bridge and another Modbus tool, against the same aGate. The symptoms are
    dropped connections, "unreachable" flapping and corrupt readings.

## Battery power rating

Force charge and discharge are sized against the battery's **power rating**.
FranklinWH doesn't report the battery model or count over Modbus, so since
0.2.8 a force command is only sent once the rating is **trusted**. That means
either the bridge has seen your battery run at the aGate's rating, or you have
**calibrated** it. See
[Settings: Site & Gateways → Battery power rating](user/ui/settings-site-gateways.md).
