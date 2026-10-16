"""Our own stale HA leftovers are removed automatically; nobody else's are."""

from __future__ import annotations

import json

from franklinwh_bridge.publish import cleanup


def _cfg(sw: str) -> bytes:
    return json.dumps({"device": {"sw_version": sw}}).encode()


def test_stale_topics_only_ours_and_not_current():
    retained = {
        # ours, currently published -> keep
        "homeassistant/sensor/franklinwh_0000abcd_battery_soc/config": _cfg("Modbus Bridge v0.2.6"),
        # ours, no longer published (type changed) -> remove
        "homeassistant/sensor/franklinwh_0000abcd_wset_enabled/config": _cfg(
            "Modbus Bridge v0.2.3"
        ),
        # v0.1.0 marker -> ours -> remove
        "homeassistant/sensor/franklinwh_0000abcd_old_thing/config": _cfg(
            "V10R01B04D00 (bridge: v0.1.0)"
        ),
        # FWHAI on an overlapping node -> never ours
        "homeassistant/sensor/franklinwh_0000abcd_home_load_kw/config": _cfg("V12 (FHAI: v0.6.79)"),
        # another site's gateway -> not our short id
        "homeassistant/sensor/franklinwh_99999999_battery_soc/config": _cfg("Modbus Bridge v0.2.6"),
        # empty = already tombstoned
        "homeassistant/sensor/franklinwh_0000abcd_gone/config": b"",
    }
    current = {"homeassistant/sensor/franklinwh_0000abcd_battery_soc/config"}
    stale = cleanup.stale_topics(retained, "franklinwh", {"0000abcd"}, current)
    assert stale == [
        "homeassistant/sensor/franklinwh_0000abcd_old_thing/config",
        "homeassistant/sensor/franklinwh_0000abcd_wset_enabled/config",
    ]


def test_old_identifiers():
    ids = cleanup.old_identifiers(
        "franklinwh", [("default", "TESTSERIAL0000ABCD"), ("Shed GW", "TESTSERIAL0000EF01")]
    )
    assert "franklinwh_TESTSERIAL0000ABCD" in ids
    assert len(ids) == 2


class _HA:
    connected = True

    def __init__(self, devices, entities):
        self.devices, self.entities, self.removed = devices, entities, []

    async def ws_call(self, msg):
        if msg["type"] == "config/device_registry/list":
            return self.devices
        if msg["type"] == "config/entity_registry/list":
            return self.entities
        if msg["type"] == "config/device_registry/remove_config_entry":
            self.removed.append(msg["device_id"])
            return None
        raise AssertionError(msg)


async def test_only_empty_devices_with_our_old_identifier_are_removed():
    old = "franklinwh_TESTSERIAL0000ABCD"
    ha = _HA(
        devices=[
            {"id": "d_empty_old", "identifiers": [["mqtt", old]], "config_entries": ["mqtt1"]},
            # merged with another integration: still has entities -> keep
            {"id": "d_merged", "identifiers": [["mqtt", old]], "config_entries": ["mqtt1"]},
            # our new device -> not an old identifier -> keep
            {
                "id": "d_new",
                "identifiers": [["mqtt", "franklinwh_modbus_TESTSERIAL0000ABCD"]],
                "config_entries": ["mqtt1"],
            },
            # someone else's empty device -> keep
            {"id": "d_other", "identifiers": [["mqtt", "fwhai_x"]], "config_entries": ["mqtt1"]},
        ],
        entities=[{"device_id": "d_merged"}, {"device_id": "d_new"}],
    )
    n = await cleanup.remove_empty_old_devices(ha, {old})
    assert n == 1 and ha.removed == ["d_empty_old"]


async def test_nothing_when_ha_not_connected():
    ha = _HA([], [])
    ha.connected = False
    assert await cleanup.remove_empty_old_devices(ha, {"x"}) == 0


def test_stale_configs_take_their_state_topics_with_them():
    """A removed entity's last value must not stay retained (#133: Command Power %)."""
    sw = {"device": {"sw_version": "Modbus Bridge v0.2.7"}}
    retained = {
        "homeassistant/number/fwh_mb_demo_battery_command_power_pct/config": json.dumps(
            {**sw, "state_topic": "fwh_mb/demo/control/battery_command_power_pct"}).encode(),
        "homeassistant/sensor/fwh_mb_demo_soc/config": json.dumps(
            {**sw, "state_topic": "fwh_mb/demo/battery/soc"}).encode(),
        # an entity that changed HA type: the old config is stale, the topic is not
        "homeassistant/sensor/fwh_mb_demo_mode/config": json.dumps(
            {**sw, "state_topic": "fwh_mb/demo/status/mode"}).encode(),
        "homeassistant/select/fwh_mb_demo_mode/config": json.dumps(
            {**sw, "state_topic": "fwh_mb/demo/status/mode"}).encode(),
        # someone else's topic named by a stale config is never touched
        "homeassistant/sensor/fwh_mb_demo_odd/config": json.dumps(
            {**sw, "state_topic": "other/demo/odd"}).encode(),
    }
    stale = ["homeassistant/number/fwh_mb_demo_battery_command_power_pct/config",
             "homeassistant/sensor/fwh_mb_demo_mode/config",
             "homeassistant/sensor/fwh_mb_demo_odd/config"]
    assert cleanup.stale_state_topics(retained, stale, "fwh_mb") == [
        "fwh_mb/demo/control/battery_command_power_pct"]
