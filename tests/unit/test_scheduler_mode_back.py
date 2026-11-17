"""Put the previous mode back when the window ends (as DC's restore_on_exit, BR-50), opt-in
on a Mode entry and safe (#27): only if the mode is still the one the entry set, never on a
write-locked gateway, and kept across a restart."""

from __future__ import annotations

from datetime import datetime

from tests.unit.test_scheduler_engine import MON, FakeHandler, entry, make_engine

AFTER = datetime(2026, 6, 15, 11, 5)  # the 10:00–11:00 window has ended


def mode_entry(restore=True):
    params = {"mode": "TOU", **({"restore_mode": True} if restore else {})}
    return entry(action="mode", params=params)


async def _run(points, handler, audits, e=None):
    eng = make_engine([e or mode_entry()], handler, points=points, audits=audits)
    await eng.tick(MON)
    assert ("operating_mode", "TOU") in handler.calls
    points["mode_name"] = "TOU"  # the gateway now reports the mode the entry set
    return eng


def _notes(audits):
    return [(a["result"], a["detail"]) for a in audits
            if a["action"] == "mode" and a["detail"].startswith("mode ")]


async def test_the_mode_it_replaced_is_put_back_when_the_window_ends():
    h, audits, points = FakeHandler(), [], {"mode_name": "Self-Consumption"}
    eng = await _run(points, h, audits)
    h.calls.clear()
    await eng.tick(datetime(2026, 6, 15, 10, 50))  # still in the window: nothing yet
    assert ("operating_mode", "Self-Consumption") not in h.calls
    await eng.tick(AFTER)
    assert ("operating_mode", "Self-Consumption") in h.calls
    assert _notes(audits) == [("ok", "mode put back to Self-Consumption (was TOU)")]
    h.calls.clear()
    await eng.tick(datetime(2026, 6, 15, 11, 10))  # once
    assert not [c for c in h.calls if c[0] == "operating_mode"]


async def test_a_mode_changed_since_is_kept():
    h, audits, points = FakeHandler(), [], {"mode_name": "Self-Consumption"}
    eng = await _run(points, h, audits)
    points["mode_name"] = "Emergency Backup"  # you, the app or another entry
    h.calls.clear()
    await eng.tick(AFTER)
    assert not [c for c in h.calls if c[0] == "operating_mode"]
    assert _notes(audits) == [("skipped", "mode not put back to Self-Consumption: changed to "
                                          "Emergency Backup since this entry set TOU, left as-is")]


async def test_a_write_locked_gateway_is_not_written():
    h, audits, points = FakeHandler(), [], {"mode_name": "Self-Consumption"}
    eng = await _run(points, h, audits)
    h.mode_write_locked = True
    h.calls.clear()
    await eng.tick(AFTER)
    assert not [c for c in h.calls if c[0] == "operating_mode"]
    assert _notes(audits)[0][0] == "skipped" and "write-locked" in _notes(audits)[0][1]


async def test_not_ticked_leaves_the_mode_as_the_entry_set_it():
    h, audits, points = FakeHandler(), [], {"mode_name": "Self-Consumption"}
    eng = await _run(points, h, audits, mode_entry(restore=False))
    h.calls.clear()
    await eng.tick(AFTER)
    assert not [c for c in h.calls if c[0] == "operating_mode"]
    assert _notes(audits) == []


async def test_a_restart_mid_window_keeps_the_first_capture():
    h, audits, points = FakeHandler(), [], {"mode_name": "Self-Consumption"}
    eng = await _run(points, h, audits)
    saved = dict(eng._mode_back)  # what the store holds
    # Restarted: the window is still open and the gateway reports TOU, the entry's mode.
    eng2 = make_engine([mode_entry()], h, points=points, audits=audits)
    eng2._mode_back = saved
    await eng2.tick(datetime(2026, 6, 15, 10, 45))
    assert list(eng2._mode_back.values())[0]["prior"] == "Self-Consumption"
    h.calls.clear()
    await eng2.tick(AFTER)
    assert ("operating_mode", "Self-Consumption") in h.calls


async def test_a_window_that_ended_while_the_bridge_was_down_is_put_back_at_start():
    h, audits, points = FakeHandler(), [], {"mode_name": "Self-Consumption"}
    eng = await _run(points, h, audits)
    eng2 = make_engine([mode_entry()], h, points=points, audits=audits)
    eng2._mode_back = dict(eng._mode_back)
    h.calls.clear()
    await eng2.tick(AFTER)
    assert ("operating_mode", "Self-Consumption") in h.calls


async def test_nothing_is_kept_when_the_mode_is_unknown_or_already_set():
    for points in ({}, {"mode_name": "TOU"}):
        h, audits = FakeHandler(), []
        eng = make_engine([mode_entry()], h, points=points, audits=audits)
        await eng.tick(MON)
        assert eng._mode_back == {}
