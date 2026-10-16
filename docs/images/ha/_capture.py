#!/usr/bin/env python3
"""Regenerate the screenshots for the Home Assistant setup guide, the battery
dispatch best-practice page and the Developer Guide.

Run against a THROWAWAY bridge only (temporary database, a mock gateway called
``demo``) and a TEST Home Assistant, never a production one. Uses the installed
Google Chrome (``channel="chrome"``), 1280x900, light theme, Sydney clock.

    python docs/images/ha/_capture.py --bridge http://127.0.0.1:8102 \
        --ha http://localhost:8180 --ha-state /tmp/ha_state.json \
        --token-file /tmp/ha_token [group ...]

Groups, in the order they were run for the docs:

- ``token``           In Home Assistant: the Create token dialog and the created
                      token, **blurred**. Writes the new token to
                      ``--token-file`` (keep it outside the repo; delete the
                      token in HA and the file afterwards).
- ``token_card``      The Long-lived access tokens card only.
- ``instance``        In the bridge: + Add Home Assistant, Test Connection, Save,
                      the connected instance, exposing ``input_boolean.away_mode``.
- ``notify``          Creates the "Doc: Away mode alert" entry (HA only, gate on
                      the toggle, Notify actions) and shoots its form.
- (by hand)           Turn the toggle on in HA and let the entry fire once.
- ``notify_log``      The Activity Log with the notifications.
- ``ha_notification`` The notification in Home Assistant's panel.
- ``ha_device``       The Mobile App integration page with the phone.
- ``dispatch``        Needs a force command running on ``demo``: Battery Control,
                      the top bar, the Release dialog, Operating Mode.
- ``dev``             Swagger UI at /docs, and Try it out sending **Release** to
                      ``demo`` (it ends the force command).

``--ha-state`` is a Playwright storage state for an already signed-in Home
Assistant session (the script never types an HA password). The Home Assistant
side needs ``input_boolean.away_mode`` and notify services
(``persistent_notification``, and a ``mobile_app_<phone>`` for the device shot).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent
W, H = 1280, 900
GW = "demo"
DOC_ENTRY = "Doc: Away mode alert"

sys.path.insert(0, str(OUT.parent / "ui"))
import _capture as _ui  # noqa: E402
from _capture import alpine, el_shot, goto, new_page  # noqa: E402

_ui.OUT = OUT  # el_shot() writes next to the module's OUT: point it here

SETTINGS = "[x-data*=settingsTab]"
#: The guides use a Sydney clock (FranklinWH's North Ryde office).
TZ = "Australia/Sydney"


class _SydneyBrowser:
    """Browser proxy whose new contexts run on the Sydney clock."""

    def __init__(self, browser) -> None:
        self._b = browser

    def new_context(self, **kw):
        kw.setdefault("timezone_id", TZ)
        return self._b.new_context(**kw)

    def __getattr__(self, name):
        return getattr(self._b, name)


def shot(page: Page, name: str, clip=None) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), clip=clip)
    print("wrote", f"{name}.png")


def api(page: Page, method: str, path: str, body=None):
    return page.evaluate(
        "async ([m, p, b]) => { const r = await fetch(p, {method: m, headers:"
        " {'Content-Type': 'application/json'}, body: b == null ? undefined : JSON.stringify(b)});"
        " const t = await r.text(); try { return JSON.parse(t); } catch (e) { return t; } }",
        [method, path, body])


# ── Home Assistant side ──────────────────────────────────────────────────


def ha_page(browser, ha: str, state: str) -> Page:
    ctx = browser.new_context(viewport={"width": W, "height": H}, storage_state=state,
                              color_scheme="light")
    page = ctx.new_page()
    page.set_default_timeout(15000)
    return page


def g_token(browser, a):
    page = ha_page(browser, a.ha, a.ha_state)
    page.goto(a.ha + "/profile/security")
    page.wait_for_timeout(4000)
    card = page.locator("ha-card", has_text="Long-lived access tokens").first
    el_shot(card, "ha-token-card", pad=8)
    card.get_by_role("button", name="Create token").click()
    page.wait_for_timeout(1200)
    dlg = page.locator("ha-dialog, ha-md-dialog, ha-wa-dialog").last
    dlg.locator("input").first.fill("FranklinWH Modbus Bridge")
    page.wait_for_timeout(500)
    shot(page, "ha-token-name")
    dlg.get_by_role("button", name="Create token").click()
    page.wait_for_timeout(2000)
    dlg = page.locator("ha-dialog, ha-md-dialog, ha-wa-dialog").last
    token = dlg.locator("input").first.input_value()
    Path(a.token_file).write_text(token)
    # Never let a real token reach a screenshot: blur the field first.
    dlg.locator("input").first.evaluate(
        "el => { el.style.filter = 'blur(6px)'; el.style.color = 'transparent';"
        " el.style.textShadow = '0 0 8px rgba(0,0,0,.6)'; }")
    page.wait_for_timeout(400)
    shot(page, "ha-token-created")
    print("token saved to", a.token_file)
    page.context.close()


def g_token_card(browser, a):
    """Only the Long-lived access tokens card (no token is created)."""
    page = ha_page(browser, a.ha, a.ha_state)
    page.goto(a.ha + "/profile/security")
    page.wait_for_timeout(4000)
    el_shot(page.locator("ha-card", has_text="Long-lived access tokens").first,
            "ha-token-card", pad=8)
    page.context.close()


def g_ha_notification(browser, a):
    page = ha_page(browser, a.ha, a.ha_state)
    page.goto(a.ha + "/lovelace/0")
    page.wait_for_timeout(5000)
    page.locator("ha-sidebar").get_by_text("Notifications", exact=True).click()
    page.wait_for_timeout(2500)
    shot(page, "ha-notification-received", clip={"x": 0, "y": 0, "width": 500, "height": 255})
    page.context.close()


def g_ha_device(browser, a):
    page = ha_page(browser, a.ha, a.ha_state)
    page.goto(a.ha + "/config/integrations/integration/mobile_app")
    page.wait_for_timeout(5000)
    shot(page, "ha-mobile-app-device")
    page.context.close()


# ── bridge side ──────────────────────────────────────────────────────────


def g_instance(browser, a):
    token = Path(a.token_file).read_text().strip()
    url = a.bridge
    page = new_page(browser, url)
    goto(page, url, "settings", "home-assistant", wait=1000)
    for inst in api(page, "GET", "api/ha/instances"):  # from an earlier run
        if inst["name"] == "Home" and not inst.get("managed"):
            api(page, "DELETE", f"api/ha/instances/{inst['id']}")
    goto(page, url, "settings", "home-assistant", wait=2000)
    card = page.locator(f"{SETTINGS} div.card:has(> div > div:text-is('Home Assistant'))").first
    card.locator("button:has-text('+ Add Home Assistant')").click()
    page.wait_for_timeout(500)
    inputs = card.locator("input[type=text], input[type=password]")
    inputs.nth(0).fill("Home")
    inputs.nth(1).fill(a.ha_url_for_bridge)
    inputs.nth(2).fill(token)
    card.locator("button:has-text('Test Connection')").click()
    page.wait_for_timeout(4000)
    el_shot(card, "ha-add-instance", pad=6)
    card.locator("button:has-text('Save')").click()
    page.wait_for_timeout(6000)
    goto(page, url, "settings", "home-assistant", wait=3000)
    card = page.locator(f"{SETTINGS} div.card:has(> div > div:text-is('Home Assistant'))").first
    el_shot(card, "ha-instance-connected", pad=6)

    goto(page, url, "ha_entities", wait=3000)
    page.locator("input[placeholder='Search entities…']").fill("away")
    page.wait_for_timeout(2000)
    row = page.locator("tr", has_text="input_boolean.away_mode").first
    if row.count():
        btn = row.locator("button").last
        exposed = api(page, "GET", "api/ha/entities?search=away")
        is_on = any(e.get("exposed") for e in exposed.get("entities") or [])
        if not is_on:
            btn.click()
            page.wait_for_timeout(1500)
    shot(page, "ha-expose-away")
    page.context.close()


def _entry_body(inst: str, targets: list[str]) -> dict:
    return {
        "name": DOC_ENTRY,
        "action": "none",
        "target_type": "gateway", "target_id": GW,
        "trigger_kind": "interval", "trigger_spec": {"every_seconds": 3600},
        "entry_conditions": {"match": "ALL", "conditions": [
            {"sensor": f"ha:{inst}:input_boolean.away_mode", "op": "==", "value": 1}]},
        "ha_actions": [
            {"kind": "notify", "instance_id": inst, "service": svc, "when": "fire",
             "title": "Away mode",
             "message": "Away mode is on. Battery at %battery.soc_pct%%, mode %mode.name%."}
            for svc in targets
        ],
    }


def g_notify(browser, a):
    url = a.bridge
    page = new_page(browser, url)
    goto(page, url, "schedule", wait=2500)
    inst = api(page, "GET", "api/ha/instances")[0]["id"]
    targets = [t["service"] for t in api(page, "GET", "api/ha/notify-targets")["targets"]]
    print("notify targets:", targets)
    want = [s for s in ("mobile_app_docs_phone", "persistent_notification") if s in targets]
    for e in api(page, "GET", "api/schedules")["schedules"]:
        if e["name"].startswith("Doc: "):
            api(page, "DELETE", f"api/schedules/{e['id']}")
    created = api(page, "POST", "api/schedules", _entry_body(inst, want))
    print("created", json.dumps(created)[:300])
    goto(page, url, "schedule", wait=2500)
    alpine(page, "[x-data*=scheduleTab]",
           f"const e = d.schedules.find(s => s.id === '{created['id']}'); d.editEntry(e);")
    page.wait_for_timeout(1500)
    page.set_viewport_size({"width": W, "height": 2000})  # room above the sticky footer
    page.wait_for_timeout(600)
    block = page.locator("div.rounded:has(> div > label:has-text('HA Actions'))").first
    el_shot(block, "ha-notify-action", pad=8)
    cond = page.locator("div.rounded:has(> div > label:has-text('Entry conditions'))").first
    if cond.count():
        el_shot(cond, "ha-away-condition", pad=8)
    alpine(page, "[x-data*=scheduleTab]", "d.closeForm ? d.closeForm() : (d.showForm = false);")
    page.context.close()


def g_notify_log(browser, a):
    url = a.bridge
    page = new_page(browser, url)
    goto(page, url, "schedule", wait=3000)
    log = page.locator("div.card:has-text('Activity Log')").last
    el_shot(log, "ha-notify-activity-log", pad=6)
    page.context.close()


def g_dispatch(browser, a):
    url = a.bridge
    page = new_page(browser, url)
    goto(page, url, "dashboard", wait=2500)
    alpine(page, "[x-data*=dashboardTab]", "")  # page ready
    page.evaluate(f"() => Alpine.store('app').setGateway ? Alpine.store('app').setGateway('{GW}')"
                  f" : (Alpine.store('app').activeGateway = '{GW}')")
    page.wait_for_timeout(3000)
    page.set_viewport_size({"width": W, "height": 2400})
    page.wait_for_timeout(800)
    bc = page.locator("[x-show='cardVisible.batteryControl']").first
    om = page.locator("[x-show='cardVisible.operatingMode']").first
    bc.screenshot(path=str(OUT / "dispatch-battery-control-active.png"))
    om.screenshot(path=str(OUT / "dispatch-operating-mode.png"))
    print("wrote dispatch-battery-control-active.png, dispatch-operating-mode.png")
    page.set_viewport_size({"width": W, "height": H})
    page.wait_for_timeout(500)
    page.evaluate("() => window.scrollTo(0, 0)")
    shot(page, "dispatch-topbar-release", clip={"x": 0, "y": 0, "width": W, "height": 70})
    page.evaluate("() => { Alpine.store('app').showReleaseModal = true; }")
    page.wait_for_timeout(1200)
    shot(page, "dispatch-release-modal")
    page.evaluate("() => { Alpine.store('app').showReleaseModal = false; }")
    page.context.close()


def g_dev(browser, a):
    url = a.bridge
    page = new_page(browser, url)
    page.goto(url + "/docs")
    page.wait_for_timeout(3500)
    shot(page, "dev-swagger-ui")
    # "Try it out" on the per-gateway command, sending Release to the demo.
    # The browser's session cookie authenticates it. Swagger UI only renders
    # what is on screen, so open the operation by its deep link on a fresh load.
    oid = "send_gateway_command_api_gateways__gw_id__command_post"
    page.goto("about:blank")
    page.goto(f"{url}/docs#/gateways/{oid}")
    page.wait_for_timeout(5000)
    op = page.locator(f"[id='operations-gateways-{oid}']")
    op.locator("button.try-out__btn").first.click()
    page.wait_for_timeout(800)
    op.locator("input[placeholder='gw_id']").fill(GW)
    op.locator("textarea.body-param__text").fill(
        '{\n  "slug": "battery_command",\n  "value": "Release"\n}')
    op.locator("button.execute").click()
    page.wait_for_timeout(2500)
    page.set_viewport_size({"width": W, "height": 2400})
    page.wait_for_timeout(500)
    op.screenshot(path=str(OUT / "dev-swagger-command.png"))
    print("wrote dev-swagger-command.png")
    page.context.close()


GROUPS = {
    "token": g_token, "token_card": g_token_card, "instance": g_instance, "notify": g_notify,
    "notify_log": g_notify_log, "ha_notification": g_ha_notification,
    "ha_device": g_ha_device,
    "dispatch": g_dispatch, "dev": g_dev,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default="http://127.0.0.1:8102")
    ap.add_argument("--ha", default="http://localhost:8180")
    ap.add_argument("--ha-url-for-bridge", default=None,
                    help="HA URL as the bridge should reach it (default: --ha)")
    ap.add_argument("--ha-state", default="")
    ap.add_argument("--token-file", default="")
    ap.add_argument("groups", nargs="*")
    a = ap.parse_args()
    a.ha_url_for_bridge = a.ha_url_for_bridge or a.ha
    with sync_playwright() as p:
        browser = _SydneyBrowser(p.chromium.launch(channel="chrome"))
        for g in a.groups or list(GROUPS):
            print("──", g)
            GROUPS[g](browser, a)
        browser.close()


if __name__ == "__main__":
    main()
