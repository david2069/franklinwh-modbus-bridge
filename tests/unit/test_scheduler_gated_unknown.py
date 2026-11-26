"""#25: an entry skipped because a reading is unknown says so, and the owner is told.

On the reference site a daily force discharge was gated "grid.connected == 1
(live=None)" for a whole night: the reading was missing, not false, and nobody
was told. Failing closed stays; the audit now names the unknown reading and the
entry's notify target(s) get one message per window.
"""

from __future__ import annotations

from tests.unit.test_scheduler_engine import GT, MON, FakeHandler, entry, make_engine


def _gated(audits):
    return [a for a in audits if a["result"] == "gated"]


def _notes(audits):
    return [a for a in audits if a["action"] == "notify"]


async def test_an_unknown_reading_is_named_and_the_owner_is_told_once():
    h, audits = FakeHandler(), []
    eng = make_engine([entry(entry_conditions={"conditions": [GT]})], h, points={}, audits=audits)
    await eng.tick(MON)
    await eng.tick(MON)  # still gated: no second audit or message
    assert h.state.active is False
    [g] = _gated(audits)
    assert "reading unknown (battery.soc_pct: no value from the gateway)" in g["detail"]
    [n] = _notes(audits)  # no notify target on the entry: audited, not silent
    assert n["detail"].startswith("SKIPPED: e1 — battery.soc_pct unknown")


async def test_a_false_reading_is_gated_as_before_with_no_message():
    h, audits = FakeHandler(), []
    eng = make_engine([entry(entry_conditions={"conditions": [GT]})], h, points={"soc": 30},
                      audits=audits)
    await eng.tick(MON)
    [g] = _gated(audits)
    assert "reading unknown" not in g["detail"]
    assert _notes(audits) == []
