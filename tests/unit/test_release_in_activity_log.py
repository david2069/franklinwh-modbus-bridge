"""A Release sent outside the scheduler shows in the schedule's Activity Log.

The top bar's Release button goes straight to the command handler, so the entry
whose command it ended had no row for it: its history stopped at "executed".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from franklinwh_bridge.api.gateways_api import send_user_command
from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db
from tests.conftest import calibrated_rating
from tests.unit.test_dispatch_verification import controller


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "d.db")
    yield conn
    await conn.close()


def engine():
    audits = []

    async def on_audit(sid, action, target, result, detail):
        audits.append({"id": sid, "action": action, "result": result, "detail": detail})

    eng = ScheduleEngine(db=None, resolver=lambda tt, tid: [], on_audit=on_audit)
    eng._entries = [{"id": "e1", "name": "Force Discharge", "action": "force_discharge"}]
    return eng, audits


def request(eng):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(schedule_engine=eng)))


async def test_handler_tracks_who_sent_the_active_command(db):
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    assert h.active_requester is None
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    assert h.active_requester == "schedule:e1"
    await h.handle_command("battery_command", "Release", requester="user:david")
    assert h.active_requester is None


async def test_user_release_of_a_schedule_command_is_in_its_activity_log(db):
    eng, audits = engine()
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    out = await send_user_command(request(eng), "default", h, "battery_command", "Release",
                                  {"username": "david"})
    assert out["ok"] is True
    assert audits == [{"id": "e1", "action": "force_discharge", "result": "released",
                       "detail": f"released by david — {out['result']}"}]


async def test_user_release_of_their_own_or_no_command_adds_no_row(db):
    eng, audits = engine()
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await send_user_command(request(eng), "default", h, "battery_command", "Release",
                            {"username": "david"})
    await send_user_command(request(eng), "default", h, "battery_command", "Force Discharge",
                            {"username": "david"})
    await send_user_command(request(eng), "default", h, "battery_command", "Release",
                            {"username": "david"})
    assert audits == []


async def test_other_commands_add_no_row(db):
    eng, audits = engine()
    h = CommandHandler(controller(), db, rating=calibrated_rating())
    await h.handle_command("battery_command", "Force Discharge", requester="schedule:e1")
    await send_user_command(request(eng), "default", h, "battery_command_target_soc", "60",
                            {"username": "david"})
    assert audits == []
    assert h.active_requester == "schedule:e1"


async def test_release_of_a_deleted_entry_still_names_its_id():
    eng, audits = engine()
    await eng.note_release("default", "schedule:gone", "user:david", "Released")
    assert audits == [{"id": "gone", "action": "", "result": "released",
                       "detail": "released by david — Released"}]
