"""Regenerate the Scheduler documentation screenshots.

Drives the installed Google Chrome (Playwright, ``channel="chrome"``) against a
THROWAWAY bridge with a mock gateway called ``demo``. It creates, edits and
deletes schedule entries, so never point it at a bridge you care about.

    source venv/bin/activate
    python docs/images/scheduler/_capture.py [--base https://127.0.0.1:8103] \\
        [--only form,examples,reference]

Viewport 1280x900, light theme, browser clock Australia/Sydney. PNGs are written
next to this script. Entries the script creates are named "Doc: …" and are
always created **disabled**: the script never saves an enabled schedule, never
presses Run now or Stop, and so never makes the engine send anything. Any "Doc:"
entries left from a previous run are deleted first.

Groups: ``form`` (the editor), ``examples`` (each example entry, opened in the
editor), ``reference`` (entry list, targets, Details, policies, Next fire) and
``list``. ``list`` captures the Activity Log and timeline of a *running* engine,
so it needs enabled entries; it is no longer in the default set and the
committed ``activity-log.png`` / ``timeline.png`` come from an earlier run.

A local bridge needs HTTPS for its login (or ALLOW_INSECURE_AUTH=1 on a LAN-only
install); for ``https://127.0.0.1`` / ``https://localhost`` with a self-signed
certificate the script tells the browser to accept it.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright

OUT = Path(__file__).resolve().parent
GW = "demo"
PREFIX = "Doc: "

# ── example entries (also used on examples.md) ─────────────────────────────


def leaf(sensor, op, value=None, value2=None, lookup=None):
    row = {"sensor": sensor, "op": op}
    if lookup:
        row.update(value_kind="sensor", value_sensor=lookup)
    else:
        row["value"] = value
        if value2 is not None:
            row["value2"] = value2
    return row


def tree(match, *conds):
    return {"match": match, "conditions": list(conds)}


def base(name, **kw):
    entry = {
        "name": PREFIX + name,
        "target_type": "gateway",
        "target_id": GW,
        "enabled": False,  # never save an enabled schedule
        "release": "release",
        "release_policy": "release",
        "conflict": "defer",
        "missed_policy": "late_fire_remaining",
        "priority": 0,
        "entry_hold_s": 0,
        "ha_actions": [],
        "params": {},
        "when_spec": {"days": [], "windows": []},
    }
    entry.update(kw)
    return entry


EXAMPLES: dict[str, dict] = {
    "first": base(
        "Evening top-up",
        when_spec={"days": [0, 1, 2, 3, 4], "windows": [{"start": "16:00", "end": "17:00"}]},
        action="force_charge", params={"power_w": 2000},
        entry_conditions=tree("ALL", leaf("battery.soc_pct", "<", 80)),
        exit_conditions=tree("ANY", leaf("battery.soc_pct", ">=", 90)),
    ),
    "peak": base(
        "Evening peak discharge",
        when_spec={"days": [], "windows": [{"start": "17:00", "end": "21:00"}]},
        action="force_discharge", params={"power_w": 5000},
        entry_conditions=tree(
            "ALL", leaf("battery.soc_pct", ">", lookup="const.min_discharge_soc"),
            leaf("service.battery_export_allowed", "==", 1)),
        exit_conditions=tree(
            "ANY", leaf("battery.soc_pct", "<=", lookup="const.min_discharge_soc")),
        priority=10,
    ),
    "spot": base(
        "Charge when spot price is low",
        trigger_kind="cron", trigger_spec={"expr": "*/30 * * * *"}, duration_s=1740,
        action="force_charge", params={"power_w": 3000},
        entry_conditions=tree(
            "ALL", leaf("tariff.spot_price", "<", 5),
            leaf("battery.soc_pct", "<", lookup="const.max_charge_soc")),
        exit_conditions=tree(
            "ANY", leaf("tariff.spot_price", ">=", 8),
            leaf("battery.soc_pct", ">=", lookup="const.max_charge_soc")),
        entry_hold_s=120,
    ),
    "storm": base(
        "Storm forecast: fill the battery",
        trigger_kind="cron", trigger_spec={"expr": "0 * * * *"}, duration_s=3540,
        action="force_charge", params={"power_w": 3000},
        entry_conditions=tree(
            "ALL", leaf("battery.soc_pct", "<", 95),
            tree("ANY", leaf("weather.code", "in", "95,96,99"),
                 leaf("weather.condition", "like", "thunder*"))),
        exit_conditions=tree("ANY", leaf("battery.soc_pct", ">=", 95)),
    ),
    "demand": base(
        "Peak-demand window: shave imports",
        trigger_kind="cron", trigger_spec={"expr": "*/30 * * * *"}, duration_s=1740,
        action="force_discharge", params={"power_w": 2500},
        entry_conditions=tree(
            "ALL", leaf("tariff.demand_window_active", "==", 1),
            leaf("grid.power_w", ">", 1000),
            leaf("battery.soc_pct", ">", lookup="const.demand_charge_min_soc")),
        exit_conditions=tree(
            "ANY", leaf("tariff.demand_window_active", "==", 0),
            leaf("battery.soc_pct", "<=", lookup="const.demand_charge_min_soc")),
        entry_hold_s=60, priority=20,
    ),
    "away": base(
        "Weekend export unless away",
        trigger_kind="weekly", trigger_spec={"time_of_day": "18:00", "days_of_week": [5, 6]},
        duration_s=7200, action="force_discharge", params={"power_w": 3000},
        entry_conditions=tree(
            "ALL", leaf("ha:home:input_boolean.away_mode", "==", 0),
            leaf("battery.soc_pct", ">", 50)),
        exit_conditions=tree(
            "ANY", leaf("battery.soc_pct", "<=", lookup="const.min_discharge_soc")),
    ),
    "eta": base(
        "Full by 07:00",
        trigger_kind="cron", trigger_spec={"expr": "*/15 0-6 * * *"}, duration_s=840,
        action="force_charge", params={},
        entry_conditions=tree(
            "ANY",
            tree("ALL", leaf("time.hour", "==", 6), leaf("battery.time_to_charge_min", ">", 0)),
            tree("ALL", leaf("time.hour", "==", 5),
                 leaf("battery.time_to_charge_min", ">=", 60)),
            tree("ALL", leaf("time.hour", "==", 4),
                 leaf("battery.time_to_charge_min", ">=", 120)),
            tree("ALL", leaf("time.hour", "<=", 3),
                 leaf("battery.time_to_charge_min", ">=", 180))),
        exit_conditions=tree(
            "ANY", leaf("battery.soc_pct", ">=", lookup="const.max_charge_soc")),
    ),
}

# ── browser helpers ────────────────────────────────────────────────────────


def login(page: Page, base_url: str) -> None:
    page.goto(base_url + "/login")
    page.fill("#u", "admin")
    page.fill("#p", "admin")
    page.click("#btn")
    page.wait_for_load_state("networkidle")
    cb = page.locator("input[type=checkbox][x-model=agreed]")
    if cb.count() and cb.first.is_visible():
        cb.first.check()
        page.get_by_role("button", name="Continue").click()
        page.wait_for_timeout(800)


def open_schedule(page: Page, base_url: str) -> None:
    page.goto(base_url + "/?tab=schedule")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)


def api(page: Page, base_url: str, method: str, path: str, body: dict | None = None):
    r = page.request.fetch(base_url + path, method=method,
                           data=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
    if not r.ok:
        raise RuntimeError(f"{method} {path}: {r.status} {r.text()}")
    return r.json()


def reset_entries(page: Page, base_url: str) -> None:
    for e in api(page, base_url, "GET", "/api/schedules")["schedules"]:
        if e["name"].startswith(PREFIX):
            api(page, base_url, "DELETE", f"/api/schedules/{e['id']}")


def modal(page: Page) -> Locator:
    return page.locator("div.relative.w-full.max-w-2xl").first


def field_select(page: Page, label: str) -> Locator:
    return modal(page).locator(f"div:has(> label.card-label:text-is('{label}')) > select").first


def section(page: Page, title: str) -> Locator:
    return modal(page).locator(f"div.rounded.border:has(> div > label:has-text('{title}'))").first


def expand_select(sel: Locator) -> None:
    """Show a native <select>'s options inline (an open native popup can't be captured)."""
    sel.evaluate("el => { el.size = el.options.length; el.style.height = 'auto';"
                 " el.style.overflow = 'hidden'; }")


def collapse_select(sel: Locator) -> None:
    sel.evaluate("el => { el.size = 0; el.style.height = ''; el.style.overflow = ''; }")


def unclip_modal(page: Page) -> None:
    """Let the dialog grow to its full height so one shot shows the whole form."""
    page.evaluate("""() => {
      const p = document.querySelector('div.relative.w-full.max-w-2xl');
      p.style.maxHeight = 'none'; p.style.overflow = 'visible';
      const wrap = p.parentElement;
      wrap.style.position = 'absolute'; wrap.style.alignItems = 'flex-start';
      wrap.style.height = 'auto'; wrap.style.minHeight = '100%';
      p.querySelectorAll('.sticky').forEach(s => s.style.position = 'static');
    }""")


def shot(target, name: str, **kw) -> None:
    target.screenshot(path=str(OUT / f"{name}.png"), **kw)
    print("wrote", name)


def union_clip(*boxes) -> dict:
    x0 = min(b["x"] for b in boxes)
    y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["width"] for b in boxes)
    y1 = max(b["y"] + b["height"] for b in boxes)
    pad = 6
    return {"x": max(0, x0 - pad), "y": max(0, y0 - pad),
            "width": x1 - x0 + 2 * pad, "height": y1 - y0 + 2 * pad}


def close_form(page: Page) -> None:
    page.keyboard.press("Escape")
    btn = modal(page).get_by_role("button", name="Cancel")
    if btn.count() and btn.first.is_visible():
        btn.first.click()
    page.wait_for_timeout(300)


def new_form(page: Page, base_url: str) -> None:
    open_schedule(page, base_url)
    page.get_by_role("button", name="+ Add Entry").click()
    page.wait_for_timeout(500)


def edit_entry(page: Page, base_url: str, name: str) -> None:
    open_schedule(page, base_url)
    page.locator("select:has(option[value=enabled])").select_option("all")
    row = page.locator("tr", has=page.locator(f"span:text-is('{name}')")).first
    row.get_by_role("button", name="Edit").click()
    page.wait_for_timeout(600)


# ── shot list ──────────────────────────────────────────────────────────────


def shots_form(page: Page, base_url: str) -> None:
    new_form(page, base_url)
    shot(page, "new-entry-dialog")

    trig = field_select(page, "Trigger Type")
    expand_select(trig)
    m = modal(page)
    shot(page, "trigger-type-dropdown", clip=union_clip(trig.bounding_box(),
                                                         m.locator("h3").bounding_box()))
    collapse_select(trig)

    preset = field_select(page, "Quick preset")
    expand_select(preset)
    shot(page, "trigger-preset-dropdown", clip=union_clip(preset.bounding_box()))
    collapse_select(preset)

    # Each trigger type's own fields: the block between the trigger row and Action.
    for kind in ("window", "once", "daily", "weekly", "interval", "monthly", "cron", "always"):
        trig.select_option(kind)
        page.wait_for_timeout(250)
        top = trig.bounding_box()
        act = field_select(page, "Action").bounding_box()
        box = {"x": top["x"], "y": top["y"], "width": m.bounding_box()["width"] - 40,
               "height": act["y"] - top["y"] - 34}
        shot(page, f"trigger-{kind}", clip=union_clip(box))
    trig.select_option("window")

    action = field_select(page, "Action")
    expand_select(action)
    shot(page, "action-dropdown", clip=union_clip(action.bounding_box()))
    collapse_select(action)
    for a, name in (("force_discharge", "action-force"), ("reserve_self", "action-reserve"),
                    ("mode", "action-mode")):
        action.select_option(a)
        page.wait_for_timeout(200)
        row = action.locator("xpath=../..")
        shot(row, name)
    action.select_option("force_charge")

    # Entry conditions: picker groups, operators, Value/Lookup, nested group.
    entry = section(page, "Entry conditions")
    entry.get_by_role("button", name="+ row").click()
    page.wait_for_timeout(200)
    entry.scroll_into_view_if_needed()
    m.evaluate("el => el.scrollTop = el.scrollTop")  # settle
    picker_btn = entry.locator("button.w-52").first
    picker_btn.evaluate("el => el.scrollIntoView({block: 'start'})")
    picker_btn.click()
    page.wait_for_timeout(300)
    dropdown = entry.locator("div.absolute.w-72").first
    groups = dropdown.locator("div.uppercase").evaluate_all(
        "els => els.map(e => e.textContent.trim())")
    for g in groups:
        slug = g.lower().replace(" / ", "-").replace(" ", "-").replace("·", "").replace("--", "-")
        dropdown.evaluate(
            """(el, label) => { const h = [...el.querySelectorAll('div.uppercase')]
                 .find(d => d.textContent.trim() === label);
               el.scrollTop = h.parentElement.offsetTop - 44; }""", g)
        page.wait_for_timeout(150)
        shot(page, f"picker-{slug}", clip=union_clip(picker_btn.bounding_box(),
                                                      dropdown.bounding_box()))
    dropdown.locator("input").fill("soc")
    page.wait_for_timeout(200)
    shot(page, "picker-filter", clip=union_clip(picker_btn.bounding_box(), dropdown.bounding_box()))
    dropdown.locator("button", has_text="Battery SOC (%)").first.click()

    op = entry.locator("select").nth(1)
    expand_select(op)
    shot(page, "operator-dropdown", clip=union_clip(entry.bounding_box(), op.bounding_box()))
    collapse_select(op)
    op.select_option("<")
    entry.locator("input[x-model='c.value']").first.fill("80")
    shot(entry, "condition-value")
    entry.get_by_role("button", name="Lookup").first.click()
    page.wait_for_timeout(200)
    entry.locator("button.w-48").first.click()
    page.wait_for_timeout(200)
    entry.locator("div.absolute.w-72:visible input").first.fill("max charge")
    page.wait_for_timeout(200)
    entry.locator("div.absolute.w-72:visible button", has_text="Max Charge SoC").first.click()
    page.wait_for_timeout(200)
    shot(entry, "condition-lookup")

    op.select_option("between")
    entry.get_by_role("button", name="Value").first.click()
    page.wait_for_timeout(150)
    entry.locator("input[x-model='c.value']").first.fill("20")
    entry.locator("input[x-model='c.value2']").first.fill("80")
    shot(entry, "condition-between")
    op.select_option("matchlist")
    page.wait_for_timeout(150)
    shot(entry, "condition-list")
    op.select_option("<")
    entry.locator("input[x-model='c.value']").first.fill("80")

    entry.get_by_role("button", name="+ group").click()
    page.wait_for_timeout(200)
    grp = entry.locator("div.border-cyan-500\\/20:visible").first
    grp.get_by_role("button", name="+ row").click()
    grp.get_by_role("button", name="+ row").click()
    page.wait_for_timeout(200)
    grp.locator("select").first.select_option("ANY")
    rows = grp.locator("div.flex-wrap:visible")
    for j, (filt, label, opv, val) in enumerate(
            (("grid power", "Grid Power (W, signed)", ">", "1000"),
             ("solar power", "Solar Power (W)", "<", "500"))):
        r = rows.nth(j)
        r.locator("button.w-52").click()
        r.locator("div.absolute.w-72:visible input").fill(filt)
        page.wait_for_timeout(150)
        r.locator("div.absolute.w-72:visible button", has_text=label).first.click()
        r.locator("select").first.select_option(opv)
        r.locator("input[x-model='cc.value']").fill(val)
    entry.locator("select[x-model='form.entry_conditions.match']").select_option("ALL")
    shot(entry, "condition-nested-group")

    entry.get_by_role("button", name="Test Verification").click()
    page.wait_for_timeout(1200)
    shot(entry, "condition-test-verification")

    shot(m.locator("div.rounded.border:has(> label:has-text('Duration'))").first, "dwell")
    shot(section(page, "HA Actions"), "ha-actions-empty")

    ex = section(page, "Exit conditions")
    ex.get_by_role("button", name="+ row").click()
    page.wait_for_timeout(200)
    ex.locator("select").nth(1).select_option(">=")
    ex.locator("input[x-model='c.value']").first.fill("95")
    shot(ex, "exit-conditions")

    tgt = field_select(page, "Target")
    tgt.evaluate("el => el.scrollIntoView({block: 'center'})")
    expand_select(tgt)
    shot(tgt.locator("xpath=../.."), "target")
    collapse_select(tgt)
    pol = field_select(page, "On exit / release").locator("xpath=../..")
    for label, name in (("On exit / release", "release-policy"),
                        ("If already controlled", "conflict-policy")):
        s = field_select(page, label)
        expand_select(s)
        shot(pol, name)
        collapse_select(s)

    trig.select_option("daily")
    page.wait_for_timeout(200)
    mp = field_select(page, "If missed during an outage")
    mp.evaluate("el => el.scrollIntoView({block: 'center'})")
    expand_select(mp)
    shot(mp.locator("xpath=.."), "missed-policy")
    collapse_select(mp)
    close_form(page)


_created_at = [0.0]


def shots_examples(page: Page, base_url: str) -> None:
    _created_at[0] = time.time()
    for body in EXAMPLES.values():
        api(page, base_url, "POST", "/api/schedules", body)
    for key, body in EXAMPLES.items():
        edit_entry(page, base_url, body["name"])
        unclip_modal(page)
        page.wait_for_timeout(300)
        shot(modal(page), f"example-{key}")
        close_form(page)


def shots_list(page: Page, base_url: str) -> None:
    open_schedule(page, base_url)
    page.locator("select:has(option[value=enabled])").select_option("all")
    page.wait_for_timeout(500)
    shot(page, "schedule-tab", full_page=True)
    row = page.locator("tr", has=page.locator(f"span:text-is('{PREFIX}Evening peak discharge')"))
    row.locator("button[title=More]").click()
    page.wait_for_timeout(300)
    shot(page, "row-menu")
    page.get_by_role("button", name="Details").first.click()
    page.wait_for_timeout(300)
    det = page.locator("div.mt-3.rounded.border:visible").first
    shot(det, "row-details")

    # The examples were created a while ago; by now the engine has evaluated
    # them (the evening window is open on the demo gateway, the others are
    # gated or skipped), so the Activity Log and the timeline have real rows.
    # Wait until dispatch verification (90 s after the send) has been logged.
    page.wait_for_timeout(max(5.0, 110 - (time.time() - _created_at[0])) * 1000)
    open_schedule(page, base_url)
    log = page.locator("div.card:has(div.card-label:text-is('Activity Log'))").first
    shot(log, "activity-log")
    tl = page.locator("div.card:has(div.card-label:has-text('Visual Timeline'))").first
    shot(tl, "timeline")


# ── reference group: list, targets, Details, policies (all entries disabled) ──

REFERENCE: dict[str, dict] = {
    "window": base(
        "Weekday solar sponge",
        when_spec={"days": [0, 1, 2, 3, 4], "windows": [{"start": "10:00", "end": "15:00"}]},
        action="force_charge", params={"power_w": 3000},
    ),
    "weekly_site": base(
        "Weekend evening export (all gateways)",
        target_type="site", target_id=None,
        trigger_kind="weekly", trigger_spec={"time_of_day": "18:00", "days_of_week": [5, 6]},
        duration_s=7200, action="force_discharge", params={"power_w": 3000},
    ),
    "cron": base(
        "Cheap spot charge",
        trigger_kind="cron", trigger_spec={"expr": "*/30 * * * *"}, duration_s=1740,
        action="force_charge", params={"power_w": 3000},
        entry_conditions=tree("ALL", leaf("tariff.spot_price", "<", 5),
                              leaf("battery.soc_pct", "<", lookup="const.max_charge_soc")),
        exit_conditions=tree("ANY", leaf("tariff.spot_price", ">=", 8),
                             leaf("battery.soc_pct", ">=", lookup="const.max_charge_soc")),
        release_policy="restore_prior_mode", entry_hold_s=120,
    ),
    "interval": base(
        "Hourly mode check",
        trigger_kind="interval", trigger_spec={"every_seconds": 3600}, action="mode",
        params={"mode": "Self-Consumption"},
    ),
    "always_ha": base(
        "Away-mode reminder (HA only)",
        trigger_kind="always", action="none",
        entry_conditions=tree("ALL", leaf("battery.soc_pct", "<", 30)),
    ),
    "monthly": base(
        "Quarterly reserve reset",
        trigger_kind="monthly", trigger_spec={"day": 1, "months": [1, 4, 7, 10],
                                              "time_of_day": "09:00"},
        action="reserve_self", params={"pct": 20},
    ),
    "priority": base(
        "Peak-demand shave (priority 20)",
        trigger_kind="daily", trigger_spec={"time_of_day": "16:00"}, duration_s=10800,
        action="force_discharge", params={"power_w": 2500},
        conflict="override", priority=20, missed_policy="late_fire_always",
    ),
}


def ensure_service(page: Page, base_url: str) -> None:
    """A utility service, so the Service target has something to list."""
    if not api(page, base_url, "GET", "/api/services").get("services"):
        api(page, base_url, "POST", "/api/services", {"name": "Ausgrid EA025"})


def shots_reference(page: Page, base_url: str) -> None:
    ensure_service(page, base_url)
    for body in REFERENCE.values():
        assert body["enabled"] is False
        api(page, base_url, "POST", "/api/schedules", body)

    open_schedule(page, base_url)
    page.locator("select:has(option[value=enabled])").select_option("all")
    page.wait_for_timeout(600)
    card = page.locator("div.card:has(div.card-label:text-is('Entries'))").first
    shot(card, "list-reference")

    # Next fire column with its header, on the bridge's clock.
    table = card.locator("table").first
    hdr = table.locator("th:text-is('Next fire')").first
    cells = table.locator("tbody tr td:nth-child(7)")
    name_hdr = table.locator("th:text-is('Name')").first
    boxes = [hdr.bounding_box(), name_hdr.bounding_box()]
    boxes += [cells.nth(i).bounding_box() for i in range(cells.count())]
    boxes += [table.locator("tbody tr td:nth-child(2)").nth(i).bounding_box()
              for i in range(cells.count())]
    shot(page, "next-fire-clock", clip=union_clip(*[b for b in boxes if b]))

    # Details panel of the cron entry (gate + exit + restore_prior_mode).
    row = page.locator("tr", has=page.locator(f"span:text-is('{PREFIX}Cheap spot charge')"))
    row.locator("button[title=More]").click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Details").first.click()
    page.wait_for_timeout(300)
    shot(page.locator("div.mt-3.rounded.border:visible").first, "details-trigger")

    # Target: Site, then Service with its list expanded.
    edit_entry(page, base_url, PREFIX + "Weekend evening export (all gateways)")
    tgt = field_select(page, "Target")
    tgt.evaluate("el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(200)
    shot(tgt.locator("xpath=../.."), "target-site")
    tgt.select_option("service")
    page.wait_for_timeout(200)
    svc = field_select(page, "Service")
    expand_select(svc)
    shot(tgt.locator("xpath=../.."), "target-service")
    collapse_select(svc)
    close_form(page)  # never saved

    # Policies row + missed policy, for a trigger entry (priority 20, override).
    edit_entry(page, base_url, PREFIX + "Peak-demand shave (priority 20)")
    pol = field_select(page, "On exit / release").locator("xpath=../..")
    mp = field_select(page, "If missed during an outage").locator("xpath=..")
    pol.evaluate("el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(200)
    shot(page, "form-policies", clip=union_clip(pol.bounding_box(), mp.bounding_box()))
    close_form(page)  # never saved


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8103")
    ap.add_argument("--only", default="form,examples,reference")
    ap.add_argument("--keep", action="store_true", help="skip the final disable pass")
    args = ap.parse_args()
    only = set(args.only.split(","))
    with sync_playwright() as p:
        local_tls = args.base.startswith(("https://127.0.0.1", "https://localhost"))
        browser = p.chromium.launch(channel="chrome", args=["--lang=en-GB"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  color_scheme="light", locale="en-GB",
                                  timezone_id="Australia/Sydney",
                                  ignore_https_errors=local_tls)
        ctx.add_init_script("try { localStorage.setItem('fwh-theme', 'light') } catch (e) {}")
        page = ctx.new_page()
        login(page, args.base)
        if only & {"examples", "list", "reference"}:
            reset_entries(page, args.base)
        if "form" in only:
            shots_form(page, args.base)
        if "examples" in only:
            shots_examples(page, args.base)
        if "reference" in only:
            shots_reference(page, args.base)
        if "list" in only:
            shots_list(page, args.base)
        if not args.keep:
            # Leave the throwaway bridge quiet: disabling releases any dispatch
            # the examples still hold on the demo gateway.
            for e in api(page, args.base, "GET", "/api/schedules")["schedules"]:
                if e["name"].startswith(PREFIX) and e["enabled"]:
                    api(page, args.base, "PATCH", f"/api/schedules/{e['id']}", {"enabled": False})
        browser.close()


if __name__ == "__main__":
    main()
