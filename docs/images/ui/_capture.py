#!/usr/bin/env python3
"""Regenerate the Dashboards & Settings screenshots for the user docs.

Run against a THROWAWAY bridge only (a temporary database with a demo
gateway). It opens dialogs and changes a few settings to show them, then puts
most of them back. It never sends a battery command, never restarts or removes
a gateway, never unpublishes and never changes the MQTT namespace: those
dialogs are opened from Alpine state and cancelled.

Usage (from the repo root, venv active):

    python docs/images/ui/_capture.py [--url http://127.0.0.1:8100] [group ...]

Groups: login, dashboard, topbar, charts, pages, settings, dialogs, config,
user, dark, guide. No group = all of them. Uses the installed Google Chrome
(``channel="chrome"``), 1280x900 viewport.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent
W, H = 1280, 900

_DISCLAIMER = "[role=dialog][aria-labelledby=disclaimer-title]"


# ── helpers ──────────────────────────────────────────────────────────────


def shot(page: Page, name: str, *, full: bool = False, clip=None) -> None:
    """Viewport screenshot, or the whole scrolled <main> when full=True."""
    path = OUT / f"{name}.png"
    if full:
        h = page.evaluate(
            "() => { const m = document.querySelector('main');"
            " return m ? m.scrollHeight + m.getBoundingClientRect().top + 8 : 900 }")
        page.set_viewport_size({"width": W, "height": int(min(max(h, H), 7000))})
        page.wait_for_timeout(700)
        page.screenshot(path=str(path))
        page.set_viewport_size({"width": W, "height": H})
        page.wait_for_timeout(300)
    else:
        page.screenshot(path=str(path), clip=clip)
    print("wrote", path.name)


def el_shot(locator, name: str, pad: int = 6) -> None:
    """Screenshot one element with a little padding around it."""
    locator.scroll_into_view_if_needed()
    page = locator.page
    page.wait_for_timeout(250)
    box = locator.bounding_box()
    if not box:
        print("SKIP (not visible)", name)
        return
    vw = page.viewport_size
    x = max(box["x"] - pad, 0)
    y = max(box["y"] - pad, 0)
    w = min(box["width"] + 2 * pad, vw["width"] - x)
    h = min(box["height"] + 2 * pad, vw["height"] - y)
    page.screenshot(path=str(OUT / f"{name}.png"),
                    clip={"x": x, "y": y, "width": w, "height": h})
    print("wrote", f"{name}.png")


def tall_el_shot(page: Page, locator, name: str) -> None:
    """Element screenshot for something taller than the viewport."""
    page.set_viewport_size({"width": W, "height": 2400})
    page.wait_for_timeout(400)
    locator.screenshot(path=str(OUT / f"{name}.png"))
    print("wrote", f"{name}.png")
    page.set_viewport_size({"width": W, "height": H})
    page.wait_for_timeout(300)


def alpine(page: Page, selector: str, js: str):
    """Run `js` with `d` bound to the Alpine data of the first `selector`."""
    return page.evaluate(
        "([sel, body]) => { const d = Alpine.$data(document.querySelector(sel));"
        " return (new Function('d', body))(d); }", [selector, js])


def goto(page: Page, url: str, tab: str | None = None, section: str | None = None,
         wait: int = 1500, hide_tz: bool = True) -> None:
    q = ""
    if tab:
        q = f"?tab={tab}" + (f"&section={section}" if section else "")
    page.goto(url + "/" + q, wait_until="domcontentloaded")
    page.wait_for_selector("[x-data*=dashboardTab]", state="attached", timeout=15000)
    dismiss_disclaimer(page)
    page.wait_for_timeout(wait)
    if hide_tz:
        page.evaluate("() => { const a = Alpine.store('app'); if (a.tz) a.tz = null; }")
        page.wait_for_timeout(200)


def dismiss_disclaimer(page: Page, capture: bool = False) -> None:
    for _ in range(12):
        if page.locator(_DISCLAIMER).count():
            break
        page.wait_for_timeout(250)
    else:
        return
    if capture:
        page.wait_for_timeout(500)
        shot(page, "disclaimer")
    page.locator(f"{_DISCLAIMER} input[type=checkbox]").first.check()
    page.locator(f"{_DISCLAIMER} button:has-text('Continue')").first.click()
    page.wait_for_timeout(800)


def login(page: Page, url: str, capture: bool = False) -> None:
    page.goto(url + "/login", wait_until="domcontentloaded")
    page.wait_for_selector("input[type=password]")
    if capture:
        page.wait_for_timeout(400)
        shot(page, "login")
    page.fill("#u", "admin")
    page.fill("#p", "admin")
    page.click("#btn")
    page.wait_for_selector("[x-data*=dashboardTab]", state="attached", timeout=15000)
    dismiss_disclaimer(page, capture=capture)
    page.wait_for_timeout(1500)


def new_page(browser, url: str, theme: str = "light", capture_login: bool = False) -> Page:
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1,
                              accept_downloads=False)
    # Theme, the dashboard defaults and the SunSpec annotations are per-browser
    # localStorage; pin them so every run renders the same thing.
    ctx.add_init_script(
        f"""try {{
          localStorage.setItem('fwh-theme', '{theme}');
          if (!sessionStorage.getItem('docs-seeded')) {{
            localStorage.removeItem('fwh-dashboard-cards');
            localStorage.removeItem('fwh-dashboard-order');
            localStorage.removeItem('fwh-topbar-prefs');
            localStorage.setItem('fwh-showSources', 'false');
            localStorage.setItem('fwh-chart-range', 'live');
            sessionStorage.setItem('docs-seeded', '1');
          }}
        }} catch (e) {{}}""")
    page = ctx.new_page()
    page.set_default_timeout(10000)
    login(page, url, capture=capture_login)
    return page


SETTINGS = "[x-data*=settingsTab]"
DASH = "[x-data*=dashboardTab]"


def card(page: Page, title: str):
    """A dashboard card by its heading text."""
    return page.locator(
        f"{DASH} div[draggable=true]:visible, {DASH} div.card:visible"
    ).filter(has=page.locator(f"text='{title}'")).first


# ── groups ───────────────────────────────────────────────────────────────


def g_login(browser, url):
    page = new_page(browser, url, capture_login=True)
    goto(page, url, "dashboard", wait=2500, hide_tz=False)
    shot(page, "timezone-banner", clip={"x": 0, "y": 0, "width": W, "height": 130})
    if not (OUT / "disclaimer.png").exists():
        # Already acknowledged: show the notice from its own component, capture
        # it, and close it without submitting anything.
        page.evaluate("() => { const el = document.querySelector('[x-data=\\'disclaimerGate()\\']');"  # noqa: E501
                      " Alpine.$data(el).open = true; }")
        page.wait_for_timeout(800)
        shot(page, "disclaimer")
        page.evaluate("() => { const el = document.querySelector('[x-data=\\'disclaimerGate()\\']');"  # noqa: E501
                      " Alpine.$data(el).open = false; }")
    page.context.close()


def g_dashboard(browser, url):
    page = new_page(browser, url)
    goto(page, url, "dashboard", wait=3500)
    shot(page, "ui-overview")
    shot(page, "dashboard-full", full=True)

    page.set_viewport_size({"width": W, "height": 2400})
    page.wait_for_timeout(600)
    for key, name in [
        ("powerFlow", "card-power-flow"), ("acPower", "card-ac-power"),
        ("batterySoc", "card-battery-soc"), ("solarInputs", "card-solar-inputs"),
        ("solarForecast", "card-solar-forecast-off"),
        ("lifetimeEnergy", "card-lifetime-energy"), ("energyFlow", "card-energy-flow"),
        ("batteryControl", "card-battery-control"), ("operatingMode", "card-operating-mode"),
    ]:
        loc = page.locator(f"{DASH} [x-show='cardVisible.{key}']").first
        loc.screenshot(path=str(OUT / f"{name}.png"))
        print("wrote", f"{name}.png")
    # The Battery card's heading is the battery label (default "Battery").
    batt = page.locator(f"{DASH} [draggable=true][x-show='cardVisible.battery']").first
    batt.screenshot(path=str(OUT / "card-battery.png"))
    print("wrote card-battery.png")
    page.set_viewport_size({"width": W, "height": H})
    page.wait_for_timeout(300)

    # Cards panel + Diagnostics
    goto(page, url, "dashboard", wait=2000)
    page.locator("button:has-text('Cards'):visible").first.click()
    page.wait_for_timeout(500)
    shot(page, "dashboard-cards-panel", clip={"x": 0, "y": 0, "width": W, "height": 520})
    page.locator("button:has-text('Cards'):visible").first.click()
    page.wait_for_timeout(300)
    page.locator("button[title=Diagnostics]:visible").first.click()
    page.wait_for_timeout(600)
    shot(page, "dashboard-diagnostics")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # Live Points (expanded) + config
    lp = page.locator(f"{DASH} [x-data='livePointsPanel()']").first
    el_shot(lp, "live-points")
    lp.locator("button:has-text('Config')").click()
    page.wait_for_timeout(500)
    tall_el_shot(page, lp, "live-points-config")
    lp.locator("button:has-text('Config')").click()
    page.wait_for_timeout(300)

    # Events History card
    ev = page.locator(f"{DASH} [x-show='cardVisible.eventsHistory']").first
    el_shot(ev, "card-events-history")

    # Sequencer bottom tab
    page.locator("button:has-text('Modbus Sequencer'):visible").first.click()
    page.wait_for_timeout(500)
    seq = page.locator(f"{DASH} [x-data='sequencerPanel()']").first
    el_shot(seq, "dashboard-sequencer-panel", pad=40)
    page.locator("button:has-text('Power History'):visible").first.click()
    page.context.close()


def g_charts(browser, url):
    page = new_page(browser, url)
    goto(page, url, "dashboard", wait=2500)
    # Let the live buffer collect a few samples so the chart has lines.
    page.wait_for_timeout(32000)
    ph = page.locator("canvas[x-ref=powerChart]").first
    ph.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    toolbar_box = page.locator("button:has-text('Expand'):visible").first.bounding_box()
    box = ph.bounding_box()
    top = toolbar_box["y"] - 70
    clip = {"x": box["x"] - 30, "y": top, "width": box["width"] + 60,
            "height": box["y"] + box["height"] - top + 20}
    page.screenshot(path=str(OUT / "power-history.png"), clip=clip)
    print("wrote power-history.png")

    # Overlays menu, with SoC ticked
    page.locator("button[title='Overlay options']:visible").first.click()
    page.wait_for_timeout(300)
    page.locator("label:has-text('SoC %'):visible").first.click()
    page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / "power-history-overlays.png"), clip=clip)
    print("wrote power-history-overlays.png")
    page.mouse.click(5, 5)
    page.wait_for_timeout(300)

    # Time span menu
    page.locator(f"{DASH} button:has(span:text-is('Live')):visible").first.click()
    page.wait_for_timeout(300)
    big = dict(clip)
    big["height"] = min(clip["height"] + 260, H - clip["y"])
    page.screenshot(path=str(OUT / "power-history-range-menu.png"), clip=big)
    print("wrote power-history-range-menu.png")
    page.mouse.click(5, 5)
    page.wait_for_timeout(300)

    # Expanded chart
    page.locator("button[title='Expand chart']:visible").first.click()
    page.wait_for_timeout(1200)
    shot(page, "power-history-expanded")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    # Compare (more than one enabled gateway)
    cmp_btn = page.locator("button[title='Compare all gateways']:visible")
    if cmp_btn.count():
        cmp_btn.first.click()
        page.wait_for_timeout(1500)
        shot(page, "power-history-compare")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

    # Events modal
    page.locator("button[title='View alarm/event history']:visible").first.click()
    page.wait_for_timeout(1000)
    shot(page, "events-modal")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    # AC chart modal: live, then history
    page.locator("button:has-text('Chart'):visible").nth(0).click()
    page.wait_for_timeout(9000)
    shot(page, "chart-ac-live")
    page.locator("button:has-text('History'):visible").first.click()
    page.wait_for_timeout(1500)
    shot(page, "chart-ac-history")
    page.locator("button:has-text('Close'):visible").first.click()
    page.wait_for_timeout(400)
    # DC chart modal
    page.locator("button:has-text('Chart'):visible").nth(1).click()
    page.wait_for_timeout(9000)
    shot(page, "chart-dc-live")
    page.locator("button:has-text('Close'):visible").first.click()
    page.context.close()


def g_topbar(browser, url):
    page = new_page(browser, url)
    goto(page, url, "dashboard", wait=2500)
    shot(page, "topbar", clip={"x": 0, "y": 0, "width": W, "height": 60})
    page.locator("header button:has(span[x-text='$store.app.activeGatewayName'])").first.click()
    page.wait_for_timeout(400)
    shot(page, "topbar-gateway-selector", clip={"x": 0, "y": 0, "width": W, "height": 330})
    page.mouse.click(700, 600)
    page.wait_for_timeout(300)
    page.locator("button[title='Topbar preferences']").first.click()
    page.wait_for_timeout(400)
    shot(page, "topbar-preferences", clip={"x": W - 520, "y": 0, "width": 520, "height": 520})
    page.mouse.click(400, 600)
    page.wait_for_timeout(300)
    page.locator("header button[title^='Signed in as']").first.click()
    page.wait_for_timeout(400)
    shot(page, "topbar-user-menu", clip={"x": W - 520, "y": 0, "width": 520, "height": 220})
    page.mouse.click(400, 600)
    page.wait_for_timeout(300)
    page.locator("header button:has-text('Release')").first.click()
    page.wait_for_timeout(700)
    shot(page, "release-modal")
    # Close with the X / Close button, never Force Release.
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    # SunSpec annotations on
    page.locator("header button[title='Toggle SunSpec model.point annotations']").first.click()
    page.wait_for_timeout(600)
    loc = page.locator(f"{DASH} [draggable=true][x-show='cardVisible.battery']").first
    loc.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    loc.screenshot(path=str(OUT / "card-battery-sunspec.png"))
    print("wrote card-battery-sunspec.png")
    page.locator("header button[title='Toggle SunSpec model.point annotations']").first.click()
    page.context.close()


def set_modules(page: Page, url: str, ids: list[str], enabled: bool) -> None:
    for mid in ids:
        page.evaluate(
            "([id, on]) => fetch('api/modules/' + id, {method: 'PATCH',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({enabled: on})})", [mid, enabled])


def g_pages(browser, url):
    page = new_page(browser, url)
    # Expert/optional pages are off on a new install: switch them on to show
    # them, then back off at the end.
    set_modules(page, url, ["explorer", "sequencer", "energy_costs"], True)
    goto(page, url, "dashboard", wait=1500)
    shot(page, "sidebar-all-modules", clip={"x": 0, "y": 0, "width": 240, "height": H})
    for tab, name in [("explorer", "page-sunspec-explorer"), ("logs", "page-logs"),
                      ("sequencer", "page-sequencer"), ("ha_entities", "page-home-assistant"),
                      ("energy_costs", "page-energy-costs")]:
        goto(page, url, tab, wait=2500)
        shot(page, name)
    page.context.close()


def g_user(browser, url):
    page = new_page(browser, url)
    page.set_viewport_size({"width": 480, "height": 900})
    page.goto(url + "/user", wait_until="domcontentloaded")
    dismiss_disclaimer(page)
    page.wait_for_timeout(4000)
    h = page.evaluate("() => document.body.scrollHeight")
    page.set_viewport_size({"width": 480, "height": int(min(h + 20, 5000))})
    page.wait_for_timeout(800)
    page.screenshot(path=str(OUT / "user-dashboard.png"))
    print("wrote user-dashboard.png")
    page.context.close()


def g_settings(browser, url):
    page = new_page(browser, url)
    for section, name in [("gateways", "settings-site-gateways"),
                          ("home-assistant", "settings-home-assistant"),
                          ("energy", "settings-energy-automation"),
                          ("data", "settings-data-backup"),
                          ("admin", "settings-admin")]:
        goto(page, url, "settings", section, wait=2500)
        shot(page, name, full=True)
    page.context.close()


def g_dialogs(browser, url):
    page = new_page(browser, url)
    goto(page, url, "settings", "gateways", wait=2500)

    # Add Gateway form (then Cancel)
    page.locator(f"{SETTINGS} button:has-text('+ Add Gateway')").first.click()
    page.wait_for_timeout(500)
    shot(page, "settings-add-gateway", clip={"x": 0, "y": 0, "width": W, "height": 640})
    page.locator(f"{SETTINGS} button:has-text('Cancel'):visible").first.click()
    page.wait_for_timeout(300)

    # Edit gateway: the unconfigured primary aGate
    alpine(page, SETTINGS, "d.startEditGw(d.gwList.find(g => g.id === 'default'))")
    page.wait_for_timeout(900)
    shot(page, "edit-gateway-default")
    alpine(page, SETTINGS, "d.editGw = null")
    page.wait_for_timeout(300)

    # Edit gateway: the demo gateway, then Test + Diagnose (read-only checks)
    alpine(page, SETTINGS, "d.startEditGw(d.gwList.find(g => g.id === 'demo'))")
    page.wait_for_timeout(900)
    modal = page.locator(f"{SETTINGS} div[x-show='editGw']").first
    modal.locator("button:text-is('Test')").click()
    page.wait_for_timeout(1500)
    modal.locator("button:text-is('Diagnose')").click()
    page.wait_for_timeout(2500)
    shot(page, "edit-gateway-demo")

    # Battery power rating + calibration panel. The section is hidden for a
    # mock gateway (a mock is always trusted), and a real calibration needs a
    # real aGate. So the panel is rendered client-side from a status object
    # shaped exactly like GET /api/gateways/{id}/battery-rating, using the
    # reason strings the bridge itself produces (gateway/battery_rating.py).
    # Nothing is sent to the bridge or the aGate; the dialog is then closed.
    hint = "Calibrate it: Settings \u2192 Site & Gateways \u2192 this gateway \u2192 Battery power rating"  # noqa: E501
    untrusted = (
        "d.editGw.mock = false; d.ratingMsg = '';"
        "d.rating = {gateway_id: 'demo', soc: 52, battery_power_w: 0, calibration: null,"
        f" charge: {{direction: 'charge', trusted: false, rating_w: null, source: 'm702_unconfirmed',"  # noqa: E501
        f" reason: 'aGate rating 5000 W not confirmed (never seen running). {hint}'}},"
        " discharge: {direction: 'discharge', trusted: true, rating_w: 10000, source: 'calibrated',"
        " reason: 'calibrated at 10000 W'}};")
    alpine(page, SETTINGS, untrusted)
    page.wait_for_timeout(500)
    rating = modal.locator("label:text-is('Battery power rating')").locator("xpath=..")
    el_shot(rating, "battery-rating", pad=10)
    alpine(page, SETTINGS,
           "d.rating = {...d.rating, soc: 52, battery_power_w: -9620,"
           " calibration: {direction: 'charge', started_at: Date.now() / 1000 - 75,"
           " elapsed_s: 75, peak_w: 9840, soc_at_start: 51}};")
    page.wait_for_timeout(500)
    el_shot(rating, "battery-rating-calibration", pad=10)
    alpine(page, SETTINGS, "d.rating = null")
    alpine(page, SETTINGS, "d.editGw = null")
    page.wait_for_timeout(300)

    # Remove gateway confirmation: opened from state, then cancelled.
    alpine(page, SETTINGS, "d.deleteGwTarget = {id: 'demo', name: 'Demo aGate'}")
    page.wait_for_timeout(500)
    shot(page, "remove-gateway-confirm")
    alpine(page, SETTINGS, "d.deleteGwTarget = null")
    page.wait_for_timeout(300)

    # Home Assistant section dialogs
    goto(page, url, "settings", "home-assistant", wait=2500)
    page.locator(f"{SETTINGS} button[data-testid=change-namespace]").first.click()
    page.wait_for_timeout(1000)
    shot(page, "change-namespace")
    page.locator("[data-testid=namespace-dialog] button:has-text('Cancel')").first.click()
    page.wait_for_timeout(400)

    alpine(page, SETTINGS, "d.confirmRepublish = true")
    page.wait_for_timeout(500)
    shot(page, "republish-confirm")
    alpine(page, SETTINGS, "d.confirmRepublish = false")
    alpine(page, SETTINGS, "d.confirmUnpublish = true")
    page.wait_for_timeout(500)
    shot(page, "unpublish-confirm")
    alpine(page, SETTINGS, "d.confirmUnpublish = false")
    page.wait_for_timeout(300)

    # MQTT broker edit form (Cancel)
    page.locator(f"{SETTINGS} button:text-is('Edit'):visible").first.click()
    page.wait_for_timeout(500)
    box = page.locator(f"{SETTINGS} [x-show=mqttEditing]").first
    el_shot(box.locator("xpath=.."), "mqtt-broker-edit", pad=8)
    page.locator(f"{SETTINGS} button:has-text('Cancel'):visible").first.click()
    page.wait_for_timeout(300)

    # Publishing group expanded
    alpine(page, SETTINGS, "return d.expandGroup('control')")
    page.wait_for_timeout(1200)
    grp = page.locator(f"{SETTINGS} div[x-show=\"expandedGroup && groupDetail\"]").first
    grp.scroll_into_view_if_needed()
    el_shot(grp.locator("xpath=.."), "publishing-groups-expanded", pad=6)
    alpine(page, SETTINGS, "return d.expandGroup('control')")

    # Add Home Assistant form (Cancel)
    page.locator(f"{SETTINGS} button:has-text('+ Add Home Assistant')").first.click()
    page.wait_for_timeout(500)
    form = page.locator(f"{SETTINGS} div:has(> div:text-is('New Instance'))").first
    el_shot(form, "ha-instance-form", pad=8)
    page.locator(f"{SETTINGS} button:has-text('Cancel'):visible").last.click()
    page.wait_for_timeout(300)

    # Dashboard card (from the MQTT card's shortcut)
    page.locator(f"{SETTINGS} button:has-text('Dashboard card'):visible").first.click()
    page.wait_for_timeout(1500)
    shot(page, "dashboard-card-dialog")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # Admin: Add user form + support info
    goto(page, url, "settings", "admin", wait=2500)
    page.locator(f"{SETTINGS} button:has-text('+ Add User')").first.click()
    page.wait_for_timeout(400)
    el_shot(page.locator("[data-testid=users-card]"), "users-add-form", pad=6)
    page.locator(f"{SETTINGS} [data-testid=users-card] button:has-text('Cancel')").first.click()
    page.locator("[data-testid=support-generate]").click()
    page.wait_for_timeout(3000)
    sup = page.locator("[data-testid=support-info-card]")
    tall_el_shot(page, sup, "support-info")
    page.context.close()


def g_config(browser, url):
    """Configure a location, NEM region and a utility service to show the
    configured states, capture, then switch the location and region back off."""
    page = new_page(browser, url)
    goto(page, url, "settings", "gateways", wait=2500)
    # Sydney CBD — a public landmark, not anybody's house.
    page.evaluate(
        "() => fetch('api/solar/settings', {method: 'PUT', headers: {'Content-Type':"
        " 'application/json'}, body: JSON.stringify({pv_latitude: -33.8688,"
        " pv_longitude: 151.2093, pv_kwp: 6.6, pv_tilt: 20, pv_azimuth: 0})})")
    page.evaluate(
        "() => fetch('api/tariff/spot/settings', {method: 'PUT', headers: {'Content-Type':"
        " 'application/json'}, body: JSON.stringify({nem_region: 'NSW1'})})")
    goto(page, url, "settings", "gateways", wait=5000)
    loc = page.locator(f"{SETTINGS} div:has(> div > div:text-is('Location & Solar'))").first
    el_shot(loc, "location-solar", pad=6)
    nem = page.locator("[data-testid=nem-card]")
    el_shot(nem, "wholesale-spot-price", pad=6)

    goto(page, url, "dashboard", wait=6000)
    fc = page.locator(f"{DASH} [x-data='solarForecastCard()']").first
    fc.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    fc.screenshot(path=str(OUT / "card-solar-forecast.png"))
    print("wrote card-solar-forecast.png")

    # A utility service with the Ausgrid two-way preset, so the tariff editor
    # and the Energy Costs page have something to show.
    goto(page, url, "settings", "energy", wait=2500)
    has_svc = alpine(page, SETTINGS, "return d.services.length")
    if not has_svc:
        page.locator(f"{SETTINGS} button:has-text('+ Add Service')").first.click()
        page.wait_for_timeout(400)
        alpine(page, SETTINGS, "d.serviceEdit.name = 'Demo service'")
        page.locator(f"{SETTINGS} button:text-is('Save'):visible").last.click()
        page.wait_for_timeout(1500)
    alpine(page, SETTINGS, "d.editService(d.services[0])")
    page.wait_for_timeout(1200)
    page.locator("button:has-text('Load preset: Ausgrid two-way'):visible").first.click()
    page.wait_for_timeout(1000)
    editor = page.locator(f"{SETTINGS} div:has(> div:text-is('Edit Service'))").first
    tall_el_shot(page, editor, "service-editor")
    for tab in ("demand", "bonus", "charge", "fixed"):
        alpine(page, SETTINGS, f"d.tariffTab = '{tab}'")
        page.wait_for_timeout(400)
        tall_el_shot(page, editor, f"service-editor-{tab}")
    alpine(page, SETTINGS, "d.tariffTab = 'import'; d.setPricingKind('dynamic')")
    page.wait_for_timeout(2500)
    tall_el_shot(page, editor, "service-editor-dynamic")
    alpine(page, SETTINGS, "d.setPricingKind('static')")
    page.wait_for_timeout(500)
    page.locator(f"{SETTINGS} button:text-is('Save'):visible").last.click()
    page.wait_for_timeout(1500)
    shot(page, "settings-energy-automation-configured", full=True)

    set_modules(page, url, ["energy_costs"], True)
    goto(page, url, "energy_costs", wait=3500)
    shot(page, "page-energy-costs-configured", full=True)

    # Put the location and spot price back off.
    page.evaluate(
        "() => fetch('api/solar/settings', {method: 'PUT', headers: {'Content-Type':"
        " 'application/json'}, body: JSON.stringify({pv_latitude: null, pv_longitude: null})})")
    page.evaluate(
        "() => fetch('api/tariff/spot/settings', {method: 'PUT', headers: {'Content-Type':"
        " 'application/json'}, body: JSON.stringify({nem_region: ''})})")
    set_modules(page, url, ["explorer", "sequencer", "energy_costs"], False)
    reset_service1(page)
    page.context.close()


def g_dark(browser, url):
    page = new_page(browser, url, theme="dark")
    goto(page, url, "dashboard", wait=3500)
    shot(page, "ui-overview-dark")
    goto(page, url, "settings", "gateways", wait=2500)
    shot(page, "settings-site-gateways-dark")
    page.context.close()


# ── worked-example guides (guide-*.png) ──────────────────────────────────

SCHED = "[x-data='scheduleTab()']"
SYD = {"lat": "-33.78667579056473", "lon": "151.12532799651834"}


def api_json(page: Page, method: str, path: str, body=None):
    return page.evaluate(
        "async ([m, p, b]) => { const r = await fetch(p, {method: m, headers:"
        " {'Content-Type': 'application/json'}, body: b == null ? undefined : JSON.stringify(b)});"
        " return r.ok ? r.json() : {error: r.status + ' ' + await r.text()}; }",
        [method, path, body])


def reset_service1(page: Page) -> None:
    """Put the seeded 'Service 1' back to a blank, static plan."""
    svc = api_json(page, "GET", "api/services")["services"]
    s1 = next((s for s in svc if s["id"] == "service1"), None)
    if s1 is None:
        return
    pricing = dict(s1.get("pricing") or {})
    pricing.update({"kind": "static", "default_rate": {"buy": 0, "sell": 0}, "seasons": [],
                    "fixed_charges": [], "export_charge": None, "export_bonus_rate": 0,
                    "billing_cycle_day": 1})
    pricing.pop("_export_charge_parked", None)
    api_json(page, "PATCH", "api/services/service1", {
        "name": "Service 1", "meter_number": "", "account": "", "ac_service": 1,
        "rated_amps": 0, "retailer": "", "network": "", "country": "", "timezone": "",
        "has_export_bonus": False, "has_peak_demand": False, "has_tou": False,
        "plan_type": "unknown", "pricing": pricing})


def sensor_check(page: Page, url: str, name: str, leaves: list) -> None:
    """Open an unsaved schedule entry, put sensors in its entry conditions,
    press Test Verification and capture the live values. Never saved."""
    goto(page, url, "schedule", wait=1500)
    js = ("d.form = d._blankForm(); d.form.name = 'docs (not saved)';"
          " d.form.entry_conditions = {match: 'ALL', conditions: "
          + "[" + ",".join(f"d._tleaf('{sid}', '{op}', {val!r})" for sid, op, val in leaves)
          + "]}; d.formOpen = true;")
    alpine(page, SCHED, js)
    page.wait_for_timeout(700)
    sec = page.locator(f"{SCHED} div.rounded.border:has(> div > label:has-text('Entry conditions'))")  # noqa: E501
    sec = sec.first
    sec.get_by_role("button", name="Test Verification").click()
    page.wait_for_timeout(1500)
    sec.scroll_into_view_if_needed()
    sec.screenshot(path=str(OUT / f"{name}.png"))
    print("wrote", f"{name}.png")
    alpine(page, SCHED, "d.closeForm()")
    page.wait_for_timeout(300)


def svc_editor(page: Page):
    return page.locator(f"{SETTINGS} div:has(> div:text-is('Edit Service'))").first


def g_guide(browser, url):
    page = new_page(browser, url)
    set_modules(page, url, ["energy_costs"], True)
    api_json(page, "PUT", "api/tariff/spot/settings", {"nem_region": ""})
    reset_service1(page)

    # 1. Location & Solar ---------------------------------------------------
    goto(page, url, "settings", "gateways", wait=2500)
    card_loc = page.locator(f"{SETTINGS} div:has(> div > div:text-is('Location & Solar'))").first
    for key, val in (("pv_latitude", SYD["lat"]), ("pv_longitude", SYD["lon"]),
                     ("pv_kwp", "6.6"), ("pv_tilt", "20"), ("pv_azimuth", "180")):
        card_loc.locator(f"[x-model='solarEdit.{key}']").fill(val)
    page.wait_for_timeout(300)
    el_shot(card_loc, "guide-location-filled", pad=6)
    card_loc.locator("button:has-text('Save')").click()
    page.wait_for_timeout(5000)
    goto(page, url, "settings", "gateways", wait=3000)
    card_loc = page.locator(f"{SETTINGS} div:has(> div > div:text-is('Location & Solar'))").first
    el_shot(card_loc, "guide-location-saved", pad=6)

    goto(page, url, "dashboard", wait=6000)
    fc = page.locator(f"{DASH} [x-data='solarForecastCard()']").first
    fc.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    fc.screenshot(path=str(OUT / "guide-solar-forecast-card.png"))
    print("wrote guide-solar-forecast-card.png")

    sensor_check(page, url, "guide-weather-sensors", [
        ("weather.temp_c", ">", 0), ("weather.condition", "!=", "Rain"),
        ("weather.code", ">=", 0),
        ("solar_forecast.today_kwh", ">", 10), ("solar_forecast.today_peak_kw", ">", 1),
        ("solar_forecast.remaining_kwh", ">", 0), ("solar_forecast.tomorrow_kwh", ">", 10),
        ("solar_forecast.day_pct", ">=", 0)])

    # 2. Flat plan: AGL Energy -------------------------------------------------
    goto(page, url, "settings", "energy", wait=2500)
    # Site name
    alpine(page, SETTINGS, "d.startSiteEdit(); d.siteEdit.name = 'Sydney office (example)'")
    page.wait_for_timeout(300)
    util = page.locator(f"{SETTINGS} div:has(> div:text-is('Electricity Utility Service(s)'))").first  # noqa: E501
    el_shot(util, "guide-site-name-edit", pad=6)
    # In 0.2.8 this Save is refused (the form sends read-only fields such as
    # updated_at); the guide says so. Clear the error toast before the next shots.
    alpine(page, SETTINGS, "return d.saveSiteConfig()")
    page.wait_for_timeout(800)
    alpine(page, SETTINGS, "d.siteEditing = false")
    page.evaluate("() => { Alpine.store('app').toasts = []; }")
    # What a NEW service form looks like (cancelled: the bridge prices from the
    # first service, so the worked example edits the one it seeded).
    alpine(page, SETTINGS, "d.addService()")
    page.wait_for_timeout(400)
    newf = page.locator(f"{SETTINGS} div:has(> div:text-is('New Service'))").first
    el_shot(newf, "guide-new-service-form", pad=6)
    alpine(page, SETTINGS, "d.cancelServiceEdit()")
    page.wait_for_timeout(300)

    alpine(page, SETTINGS, "d.editService(d.services.find(s => s.id === 'service1'))")
    page.wait_for_timeout(1000)
    alpine(page, SETTINGS, """
      const e = d.serviceEdit;
      e.name = 'AGL Energy — main meter'; e.meter_number = '4103000000 (example NMI)';
      e.account = '1234 5678 (example)'; e.ac_service = 1; e.rated_amps = 63;
      e.network = 'Ausgrid'; e.pto_status = 'approved'; e.retailer = 'AGL Energy';
      e.country = 'AU'; e.timezone = 'Australia/Sydney';
      e.has_export_bonus = false; e.has_peak_demand = false; e.has_export_charge = false;
      e.pricing.billing_cycle_day = 1;
      d.setPricingKind('static');
      d.defaultRate.buy = 0.33; d.defaultRate.sell = 0.05;
      e.pricing.fixed_charges = [{type: 'supply', description: 'Daily supply charge (example)',
        levied_by: 'retailer', frequency: 'daily', rate: 1.10, tax_rate: 0}];
      d.tariffTab = 'import'; d.checkRates();""")
    page.wait_for_timeout(1200)
    ed = svc_editor(page)
    tall_el_shot(page, ed, "guide-agl-service")
    alpine(page, SETTINGS, "d.tariffTab = 'fixed'")
    page.wait_for_timeout(400)
    fixed = ed.locator("div[x-show=\"tariffTab === 'fixed'\"]").first
    el_shot(fixed, "guide-agl-standing-charge", pad=6)
    alpine(page, SETTINGS, "d.tariffTab = 'import'")
    page.wait_for_timeout(400)
    rates = ed.locator("div[x-show=\"tariffTab === 'import'\"]").first
    el_shot(rates, "guide-agl-flat-rates", pad=6)
    alpine(page, SETTINGS, "return d.saveService()")
    page.wait_for_timeout(1500)
    alpine(page, SETTINGS, "d.cancelServiceEdit()")
    page.wait_for_timeout(500)
    util = page.locator(f"{SETTINGS} div:has(> div:text-is('Electricity Utility Service(s)'))").first  # noqa: E501
    el_shot(util, "guide-agl-service-list", pad=6)

    # Assigning the gateway: shown, then cancelled (gateways are not changed).
    goto(page, url, "settings", "gateways", wait=2500)
    alpine(page, SETTINGS,
           "d.startEditGw(d.gwList.find(g => g.id === 'default')); d.editGw.service_id = 'service1'")  # noqa: E501
    page.wait_for_timeout(900)
    shot(page, "guide-gateway-service")
    alpine(page, SETTINGS, "d.editGw = null")

    goto(page, url, "energy_costs", wait=3500)
    shot(page, "guide-agl-energy-costs", full=True)
    sensor_check(page, url, "guide-agl-sensors", [
        ("tariff.buy_rate", ">", 0), ("tariff.sell_rate", ">", 0),
        ("tariff.spot_price", ">", -1000), ("energy.import_cost", ">=", 0),
        ("energy.export_credit", ">=", 0), ("fixed.daily_charge", ">", 0)])

    # 3. Dynamic plan: Amber Electric on the NEM spot ---------------------------
    goto(page, url, "settings", "gateways", wait=2500)
    page.locator("[data-testid=nem-region]").select_option("NSW1")
    page.wait_for_timeout(6000)
    goto(page, url, "settings", "gateways", wait=4000)
    el_shot(page.locator("[data-testid=nem-card]"), "guide-nem-card", pad=6)

    goto(page, url, "settings", "energy", wait=2500)
    alpine(page, SETTINGS, "d.editService(d.services.find(s => s.id === 'service1'))")
    page.wait_for_timeout(1000)
    alpine(page, SETTINGS, """
      const e = d.serviceEdit;
      e.name = 'Amber Electric — main meter'; e.retailer = 'Amber Electric';
      e.pricing.fixed_charges = [
        {type: 'supply', description: 'Network daily supply (example)', levied_by: 'utility',
         frequency: 'daily', rate: 1.10, tax_rate: 0},
        {type: 'membership', description: 'Amber membership (example)', levied_by: 'retailer',
         frequency: 'monthly', rate: 25, tax_rate: 0}];
      d.tariffTab = 'import'; d.setPricingKind('dynamic');
      e.pricing.dynamic.source = 'nem'; e.pricing.dynamic.region = 'NSW1';
      return d.dynamicChanged();""")
    page.wait_for_timeout(4000)
    ed = svc_editor(page)
    rates = ed.locator("div[x-show=\"tariffTab === 'import'\"]").first
    el_shot(rates, "guide-amber-dynamic", pad=6)
    tall_el_shot(page, ed, "guide-amber-service")
    alpine(page, SETTINGS, "return d.saveService()")
    page.wait_for_timeout(1500)
    alpine(page, SETTINGS, "d.cancelServiceEdit()")

    goto(page, url, "energy_costs", wait=4000)
    shot(page, "guide-amber-energy-costs", full=True)
    sensor_check(page, url, "guide-amber-sensors", [
        ("tariff.spot_price", ">", -1000), ("tariff.feed_in_price", ">", -1000),
        ("tariff.buy_rate", ">", -10), ("tariff.sell_rate", ">", -10),
        ("tariff.time_period", "!=", "x"), ("energy.unpriced_import_kwh", ">=", 0)])

    # Put the bridge back.
    reset_service1(page)
    api_json(page, "PATCH", "api/site", {"name": "My Site"})
    api_json(page, "PUT", "api/tariff/spot/settings", {"nem_region": ""})
    api_json(page, "PUT", "api/solar/settings", {"pv_latitude": None, "pv_longitude": None,
                                                 "pv_kwp": 5, "pv_tilt": 20, "pv_azimuth": 0})
    set_modules(page, url, ["explorer", "sequencer", "energy_costs"], False)
    page.context.close()


GROUPS = {
    "login": g_login, "dashboard": g_dashboard, "topbar": g_topbar, "charts": g_charts,
    "pages": g_pages, "settings": g_settings, "dialogs": g_dialogs, "config": g_config,
    "user": g_user, "dark": g_dark, "guide": g_guide,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8100")
    ap.add_argument("groups", nargs="*")
    args = ap.parse_args()
    url = args.url.rstrip("/")
    todo = args.groups or list(GROUPS)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for g in todo:
            print(f"── {g}")
            GROUPS[g](browser, url)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
