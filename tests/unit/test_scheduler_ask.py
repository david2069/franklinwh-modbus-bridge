"""#499 step 2 (as DC #576): a schedule's question with buttons. The run waits for
the answer; Yes runs it, No skips it (battery action and HA actions), no reply follows
"if no reply"; asked once per run, never again after a restart."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from franklinwh_ha_notify import Answer

from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from tests.unit.test_scheduler import _ALLDAY, FakeHandler, _entry

T0 = datetime(2026, 6, 15, 10, 0, 0)
T1 = datetime(2026, 6, 15, 10, 0, 15)
T2 = datetime(2026, 6, 15, 10, 0, 30)


class FakeAsker:
    ready = True

    def __init__(self, answer: Answer | None = None, reach: bool = True):
        self.answer = answer or Answer(None, timed_out=True)
        self.reach = reach
        self.asked: list[dict] = []
        self.open: dict[str, object] = {}

    async def ask_schedule(self, **kw):
        self.asked.append(kw)
        if not self.reach:
            return None, "no phone that shows buttons"
        return SimpleNamespace(id=f"r{len(self.asked)}"), ""

    def open_request(self, run):
        return self.open.get(run)

    async def wait(self, req, timeout_s):
        await asyncio.sleep(0)
        return self.answer


def _ask(**kw):
    row = {"kind": "notify", "actionable": True, "when": "fire", "title": "Charge?",
           "message": "Charge now? SoC %battery.soc_pct%%", "timeout_min": 5,
           "if_no_reply": "skip", "choices": [], "send_to": {"type": "all"}}
    row.update(kw)
    return row


def _notify_row(msg="Answer was %ask.answer%"):
    return {"kind": "notify", "instance_id": "ha1", "service": "phone", "when": "fire",
            "message": msg}


def _engine(entry, asker, handler):
    sent, outcomes = [], []

    async def notify(inst, svc, title, message):
        sent.append(message)
        return {"ok": True}

    eng = ScheduleEngine(db=None, resolver=lambda tt, tid: [("default", handler)],
                         points_fn=lambda gw: {"soc": 42}, ha_notify_fn=notify, asker=asker)

    async def on_outcome(event, name, gw, detail):
        outcomes.append((event, detail))

    eng.on_outcome = on_outcome
    eng._entries = [entry]
    return eng, sent, outcomes


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_yes_runs_the_action_and_the_entry_rows_after_the_answer():
    h = FakeHandler()
    asker = FakeAsker(Answer("C0"))
    e = _entry(when_spec=_ALLDAY, ha_actions=[_ask(), _notify_row()])
    eng, sent, _ = _engine(e, asker, h)
    await eng.tick(T0)
    assert h.calls == [] and sent == []          # held while the question is open
    await _settle()
    await eng.tick(T1)
    assert ("battery_command", "Force Charge") in h.calls
    assert sent == ["Answer was Yes"]             # %ask.answer% filled
    assert len(asker.asked) == 1
    assert asker.asked[0]["question"] == "Charge now? SoC 42% (reply within 5 min)"
    await eng.tick(T2)
    assert len(asker.asked) == 1                  # asked once per run


async def test_no_skips_the_battery_action_and_every_ha_row():
    h = FakeHandler()
    asker = FakeAsker(Answer("C1"))
    exit_row = {**_notify_row("bye"), "when": "exit"}
    e = _entry(when_spec=_ALLDAY, ha_actions=[_ask(), _notify_row(), exit_row])
    eng, sent, _ = _engine(e, asker, h)
    await eng.tick(T0)
    await _settle()
    await eng.tick(T1)
    await eng.tick(T2)
    assert h.calls == [] and sent == []
    await eng.tick(datetime(2026, 6, 15, 18, 30))  # window over: no exit row either
    assert sent == []
    assert len(asker.asked) == 1


async def test_no_reply_follows_if_no_reply_and_tells_the_scheduler_group():
    for rule, fired in (("skip", False), ("continue", True)):
        h = FakeHandler()
        e = _entry(when_spec=_ALLDAY, ha_actions=[_ask(if_no_reply=rule)])
        eng, _, outcomes = _engine(e, FakeAsker(), h)
        await eng.tick(T0)
        await _settle()
        await eng.tick(T1)
        await _settle()
        assert (("battery_command", "Force Charge") in h.calls) is fired, rule
        assert [o[0] for o in outcomes] == ["schedule_no_reply"]


async def test_own_buttons_and_a_typed_reply_become_the_answer():
    h = FakeHandler()
    choices = [{"label": "Charge", "then": "run"}, {"label": "Later", "then": "skip"},
               {"label": "Note", "then": "run", "reply": True}]
    e = _entry(when_spec=_ALLDAY, ha_actions=[_ask(choices=choices), _notify_row()])
    eng, sent, _ = _engine(e, FakeAsker(Answer("C2", reply="only 3 kW")), h)
    await eng.tick(T0)
    await _settle()
    await eng.tick(T1)
    assert sent == ["Answer was only 3 kW"]
    assert ("battery_command", "Force Charge") in h.calls


async def test_nobody_reachable_decides_at_once_and_notifications_starting_waits():
    h = FakeHandler()
    asker = FakeAsker(reach=False)
    e = _entry(when_spec=_ALLDAY, ha_actions=[_ask(if_no_reply="continue")])
    asker.ready = False
    eng, _, outcomes = _engine(e, asker, h)
    await eng.tick(T0)
    await _settle()
    assert asker.asked == [] and h.calls == []    # waits for notifications to start
    asker.ready = True
    await eng.tick(T1)
    await _settle()
    await eng.tick(T2)
    assert ("battery_command", "Force Charge") in h.calls
    assert outcomes == []                         # not a "no reply": nobody was asked


async def test_a_decided_run_is_not_asked_again_after_a_restart():
    h = FakeHandler()
    asker = FakeAsker(Answer("C0"))
    e = _entry(when_spec=_ALLDAY, ha_actions=[_ask()])
    eng, _, _ = _engine(e, asker, h)
    await eng.tick(T0)
    await _settle()
    await eng.tick(T1)
    runs = dict(eng._ask_runs)
    h2 = FakeHandler()
    eng2, _, _ = _engine(e, asker, h2)            # a new engine after a restart
    eng2._ask_runs = runs
    await eng2.tick(T2)
    assert len(asker.asked) == 1
    assert ("battery_command", "Force Charge") in h2.calls


async def test_a_question_sent_before_a_restart_is_waited_on_not_sent_again():
    h = FakeHandler()
    asker = FakeAsker(Answer("C0"))
    e = _entry(when_spec=_ALLDAY, ha_actions=[_ask()])
    eng, _, _ = _engine(e, asker, h)
    asker.open["default:e1@2026-06-15T09:00:00"] = SimpleNamespace(id="old")
    await eng.tick(T0)
    await _settle()
    await eng.tick(T1)
    assert asker.asked == []
    assert ("battery_command", "Force Charge") in h.calls


async def test_a_chosen_phone_that_is_switched_off_is_not_asked(tmp_path):
    """As DC: Send to chosen phones asks the enabled ones among them; a switched-off
    phone stays off (it was asked anyway)."""
    from franklinwh_ha_notify import Target

    from franklinwh_bridge.actionable import Actionable
    from franklinwh_bridge.store.db import create_notify_device, init_db

    db = await init_db(tmp_path / "ask.db")
    on = await create_notify_device(db, "Pixel", "ha1", "mobile_app_pixel", kind="phone")
    off = await create_notify_device(db, "iPad", "ha1", "mobile_app_ipad", enabled=False,
                                     kind="phone")
    sent = []

    class FakeNotifier:
        instances = {"ha1": object()}

        async def ask(self, event, targets, buttons, **kw):
            sent.append([t.service for t in targets])
            return SimpleNamespace(id="r1")

    act = Actionable(db, tmp_path / "unused.db")
    act.notifier = FakeNotifier()
    act._enabled = True
    yes_no = [{"label": "Yes"}, {"label": "No"}]
    kw = {"title": "t", "question": "q", "choices": yes_no, "ttl_s": 60, "gateway": "gw",
          "run": "r"}
    req, _ = await act.ask_schedule(send_to={"type": "device", "ids": [on["id"], off["id"]]},
                                    **kw)
    assert req.id == "r1" and sent == [["mobile_app_pixel"]]
    req, why = await act.ask_schedule(send_to={"type": "device", "ids": [off["id"]]}, **kw)
    assert req is None and why == "no phone that shows buttons"
    assert Target("ha1", "mobile_app_pixel", "Pixel").buttons
    await db.close()
