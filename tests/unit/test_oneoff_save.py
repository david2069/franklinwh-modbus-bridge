"""A one-off trigger keeps its fire_at through the editor's save, and an enabled entry
that would never fire is refused at save time (#337). Before the fix the editor had
no one-off form, so a save sent trigger_spec {} and the entry silently never fired."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.unit.test_schedules_v2_api import client  # noqa: F401  (fixture)

FUTURE = (datetime.now() + timedelta(days=3)).replace(second=0, microsecond=0)
PAST = FUTURE - timedelta(days=30)


def oneoff(fire_at, **kw):
    return {"name": "storm fill", "action": "force_charge", "trigger_kind": "oneoff",
            "trigger_spec": {"fire_at": fire_at}, "duration_s": 3600, **kw}


async def test_oneoff_without_fire_at_is_refused(client):  # noqa: F811
    created = (await client.post("/api/schedules", json=oneoff(FUTURE.isoformat()))).json()
    # What the old editor sent on save: the trigger kind with an empty spec.
    r = await client.patch(f"/api/schedules/{created['id']}",
                           json={"trigger_kind": "oneoff", "trigger_spec": {}})
    assert r.status_code == 400 and "fire_at" in r.json()["detail"]
    kept = (await client.get(f"/api/schedules/{created['id']}")).json()
    assert kept["trigger_spec"]["fire_at"] == FUTURE.isoformat()


async def test_oneoff_saved_from_the_editor_keeps_its_time(client):  # noqa: F811
    created = (await client.post("/api/schedules", json=oneoff(FUTURE.isoformat()))).json()
    later = FUTURE + timedelta(hours=1)
    # What the editor now sends: date and time from the form, as fire_at.
    r = await client.patch(f"/api/schedules/{created['id']}", json={
        "trigger_kind": "oneoff",
        "trigger_spec": {"fire_at": f"{later:%Y-%m-%d}T{later:%H:%M}:00"},
        "when_spec": {"days": [], "windows": []}, "duration_s": 3600, "enabled": True,
    })
    assert r.status_code == 200
    assert r.json()["next_fire"] == later.isoformat()


async def test_enabled_entry_that_would_never_fire_is_refused(client):  # noqa: F811
    r = await client.post("/api/schedules", json=oneoff(PAST.isoformat()))
    assert r.status_code == 400 and "never fire" in r.json()["detail"]
    r = await client.post("/api/schedules", json={
        "name": "old date", "action": "force_charge",
        "when_spec": {"date": f"{PAST:%Y-%m-%d}", "windows": [{"start": "05:00", "end": "06:00"}]}})
    assert r.status_code == 400


async def test_a_past_entry_can_be_kept_disabled_and_renamed(client):  # noqa: F811
    r = await client.post("/api/schedules", json=oneoff(PAST.isoformat(), enabled=False))
    assert r.status_code == 201
    sid = r.json()["id"]
    assert (await client.patch(f"/api/schedules/{sid}", json={"name": "x"})).status_code == 200
    r = await client.patch(f"/api/schedules/{sid}", json={"enabled": True})
    assert r.status_code == 400


async def test_editor_has_a_oneoff_form_that_writes_fire_at():
    from pathlib import Path

    js = (Path(__file__).parents[2] / "src/franklinwh_bridge/static/js/schedule_tab.js").read_text()
    assert "id: 'oneoff'" in js
    assert "fire_at: `${f.date}T${f.time_of_day}:00`" in js
