"""Unmapped register 15016: log each change with context, to map it (#36)."""

from __future__ import annotations

import logging

from franklinwh_bridge.modbus.register_watch import RegisterWatch
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES


def test_logs_first_value_and_each_change_with_context(caplog):
    w = RegisterWatch("gw")
    base = {"mode_name": "Self-Consumption", "battery_state": "Discharging",
            "grid_mode": "Grid Following", "soc": 90}
    with caplog.at_level(logging.INFO):
        w.apply({**base, "vreg_15016": 6})
        w.apply({**base, "vreg_15016": 6})  # unchanged: nothing
        w.apply({**base, "vreg_15016": 2, "mode_name": "TOU"})
    msgs = [m for m in caplog.messages if "unmapped register 15016" in m]
    assert len(msgs) == 2
    assert "start → 6" in msgs[0] and "mode_name=Self-Consumption" in msgs[0]
    assert "6 → 2" in msgs[1] and "mode_name=TOU" in msgs[1]


def test_unread_register_is_not_a_change(caplog):
    w = RegisterWatch("gw")
    with caplog.at_level(logging.INFO):
        w.apply({"vreg_15016": 6})
        w.apply({})  # read failed this poll
        w.apply({"vreg_15016": 6})
    assert sum("unmapped register 15016" in m for m in caplog.messages) == 1


def test_raw_value_is_a_diagnostic_entity_labelled_unconfirmed():
    e = {x.slug: x for x in BRIDGE_ENTITIES}["ext_15016_raw"]
    assert e.stat_key == "vreg_15016" and e.entity_category == "diagnostic"
    assert "unconfirmed" in e.name
