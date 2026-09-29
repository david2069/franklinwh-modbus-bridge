#!/usr/bin/env python3
"""Browser console gate — fail on ANY console error or uncaught exception.

Drives the running Bridge UI with Playwright and asserts the console stays
clean. Uses the Google Chrome already installed on the machine
(``channel="chrome"``) rather than downloading a browser.

Why not Safari: ``safaridriver`` ships with macOS and can drive clicks, but
Safari implements none of WebDriver's log endpoints, so the console cannot be
read. Injecting a ``window.onerror`` hook after load would miss precisely the
load-time errors this gate exists to catch.

Listeners are attached BEFORE the first navigation so nothing is missed.
Modals are opened, closed and reopened, because Alpine teardown bugs surface on
the second open, not the first.

Usage:
    ./venv/bin/python3 tools/console_gate.py [--url URL] [--user U] [--password P]

Exit code 0 = clean, 1 = errors found (and they are printed).
"""

from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

# Known noise that says nothing about the change under test.
IGNORE_SUBSTRINGS = (
    "favicon.ico",  # no favicon is served; not a code defect
    # We self-host Tailwind, but it is the Play/CDN build, which prints this
    # on every page regardless of origin. Ignored so the gate stays useful —
    # NOT ignored as a non-issue: that build JITs CSS in the browser and is
    # the same one implicated in FWHAI's mobile Safari crash. Replacing it
    # with a compiled stylesheet is its own piece of work.
    "cdn.tailwindcss.com should not be used in production",
)


def _should_ignore(text: str) -> bool:
    return any(s in text for s in IGNORE_SUBSTRINGS)


def _goto(page, url: str, problems: list) -> bool:
    """Navigate, tolerating the one flaky failure mode this app produces.

    The SPA can start its own navigation while Playwright is still waiting for
    networkidle, which surfaces as ERR_ABORTED. That is a race in the harness,
    not a defect in the page — but left unhandled it makes the gate fail at
    random, and a gate that cries wolf gets ignored. One retry on a laxer wait
    condition; a second failure is reported rather than swallowed.
    """
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="networkidle" if attempt == 1 else "domcontentloaded")
            return True
        except Exception as exc:
            if attempt == 2:
                problems.append(f"navigation failed: {url} ({exc})")
                return False
            page.wait_for_timeout(600)
    return False


def _wait_until_ready(url: str, settle_s: float = 6.0, timeout_s: float = 90.0) -> str | None:  # noqa: E501
    """Block until the app has been up for `settle_s`, or report why not.

    Editing anything under src/ — including static JS — restarts uvicorn, and a
    gate run that lands mid-restart reports missing buttons that are merely not
    mounted yet. That produced three false failures in a row, which is worse
    than no gate: the habit it teaches is "re-run until green", and then a real
    failure gets re-run away too.
    """
    import json
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=3) as r:
                uptime = json.loads(r.read()).get("uptime_s", 0)
            if uptime >= settle_s:
                # Confirm it is STILL settled a moment later. A single check
                # passes on a process that is about to be replaced: the reload
                # begins after the probe and the run then reports buttons that
                # simply aren't mounted yet. Two agreeing samples is the
                # cheapest way to catch that.
                time.sleep(2.0)
                with urllib.request.urlopen(f"{url}/api/health", timeout=3) as r2:
                    if json.loads(r2.read()).get("uptime_s", 0) >= uptime:
                        return None
                continue
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1.5)
    return f"app never settled for {settle_s}s within {timeout_s}s"


#: Present only once the SPA shell has actually mounted. Used as the proof that
#: login worked, because the URL is not one: a successful submit can leave the
#: browser sitting on /login while the app renders underneath.
_APP_ROOT = "[x-data*=dashboardTab]"


def _login(page, url: str, user: str, password: str, problems: list) -> bool:
    """Log in and confirm the app actually mounted.

    Submitting and calling it done is not enough. networkidle can return before
    the session is established, and the next navigation then redirects back to
    /login — so every element lookup that follows fails and the gate reports a
    dozen missing buttons for a UI that is fine. That is indistinguishable from
    a real regression at the point of reading the output, and it is what teaches
    people to re-run a gate until it goes green.

    So: submit, then verify the shell is present, retrying the whole sequence a
    couple of times before declaring the run unable to proceed. A gate that
    cannot run must say so rather than emit findings.
    """
    for attempt in (1, 2, 3):
        if page.locator("input[type=password]").count():
            page.fill("input[name=username], input[type=text]", user)
            page.fill("input[type=password]", password)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

        for _ in range(8):
            page.wait_for_timeout(500)
            if page.locator(_APP_ROOT).count():
                return True
        _goto(page, url, problems)

        if attempt < 3:
            page.wait_for_timeout(1000)

    problems.append(
        f"login never mounted the app shell ({_APP_ROOT} absent) — "
        f"check the credentials passed via --user/--password"
    )
    return False


def run(url: str, user: str, password: str) -> int:
    problems: list[str] = []

    not_ready = _wait_until_ready(url)
    if not_ready:
        print(f"CONSOLE GATE COULD NOT RUN — {not_ready}")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_default_timeout(8000)  # fail fast; this is a gate, not a crawl

        # Attach BEFORE any navigation.
        # Check the message's LOCATION as well as its text: a failed subresource
        # logs a generic "Failed to load resource ... 404" and carries the URL
        # only in location, so filtering on text alone can neither identify it
        # nor ignore it precisely.
        page.on("console", lambda m: (
            problems.append(
                f"console.{m.type}: {m.text} @ {(m.location or {}).get('url', '?')}"
            )
            if m.type in ("error", "warning")
            and not _should_ignore(m.text)
            and not _should_ignore((m.location or {}).get("url", ""))
            else None
        ))
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        # ERR_ABORTED on a DOCUMENT request is a superseded navigation — the
        # SPA started its own while this one was in flight. Same harness race
        # as in _goto(). Narrow on purpose: an aborted script/fetch/image is
        # still reported, and any other document failure still fails the gate.
        page.on("requestfailed", lambda r: (
            problems.append(f"requestfailed: {r.url} ({r.failure})")
            if not _should_ignore(r.url)
            and not (r.resource_type == "document" and "ERR_ABORTED" in (r.failure or ""))
            else None
        ))
        # A 404 is a completed response, not a "failed request" — without this
        # the console shows a bare "404" with no way to tell WHICH resource.
        page.on("response", lambda r: (
            problems.append(f"http {r.status}: {r.url}")
            if r.status >= 400 and not _should_ignore(r.url) else None
        ))

        _goto(page, url, problems)

        if not _login(page, url, user, password, problems):
            print(f"CONSOLE GATE COULD NOT RUN — {problems[-1]}")
            browser.close()
            return 1

        for tab in ("dashboard", "schedule", "settings", "logs", "events"):
            _goto(page, f"{url}?tab={tab}", problems)
            page.wait_for_timeout(900)

        # Schedule → timeline Appearance panel. Opened twice for the same
        # teardown reason, and every preset is clicked: each one rewrites CSS
        # variables that the whole timeline re-reads.
        _goto(page, f"{url}?tab=schedule", problems)
        page.wait_for_timeout(1200)
        for _ in (1, 2):
            btn = page.locator("button[aria-label='Timeline appearance']:visible").first
            if not btn.count():
                problems.append("schedule: timeline Appearance button missing")
                break
            btn.click()
            page.wait_for_timeout(350)
            for preset in ("Auto (match theme)", "High contrast", "Muted", "Flat (no track)"):
                opt = page.locator(f"button:has-text('{preset}'):visible").first
                if opt.count():
                    opt.click()
                    page.wait_for_timeout(220)
            for slider in page.locator("input[type=range]:visible").all():
                slider.fill("0.5")
                page.wait_for_timeout(150)
            reset = page.locator("button:has-text('Reset'):visible").first
            if reset.count():
                reset.click()
            page.keyboard.press("Escape")
            page.mouse.click(5, 5)  # click-outside closes the panel
            page.wait_for_timeout(300)

        # Settings → open the service editor twice. The second open is the one
        # that catches teardown bugs.
        _goto(page, f"{url}?tab=settings", problems)
        for attempt in (1, 2):
            # Scope to the SERVICE row: other tabs stay in the DOM hidden, and
            # Settings itself has gateway Edit buttons too — an unscoped match
            # opens the wrong editor and the preset buttons legitimately aren't
            # there, which reads as a false failure. The delete-service button
            # is the stable landmark for the row.
            row = page.locator("div:has(> div > div > button[title='Delete service'])")
            edit = row.locator("button:has-text('Edit'):visible").first
            if not edit.count():
                problems.append("settings: no visible Edit button — UI changed?")
                break
            edit.click()
            page.wait_for_timeout(700)

            # Service location fields (migration 38). Presence only — a console
            # gate can't tell you they're WIRED, just that they're still there.
            if attempt == 1:
                for field in ("country", "timezone"):
                    if not page.locator(f"[x-model='serviceEdit.{field}']:visible").count():
                        problems.append(f"settings: service {field} field missing")

            # Exercise the tariff presets, which is what this run is gating.
            for label in ("Load preset: AGL TOU", "Load preset: Ausgrid two-way"):
                btn = page.locator(f"button:has-text('{label}'):visible")
                if btn.count():
                    btn.first.click()
                    page.wait_for_timeout(400)
                elif attempt == 1:
                    problems.append(f"settings: preset button missing — {label}")

            cancel = page.locator("button:has-text('Cancel'):visible").first
            if cancel.count():
                cancel.click()
            page.wait_for_timeout(500)

        # Dashboard → the AC/DC metrics chart modal, in BOTH modes. Opened
        # twice for the usual teardown reason, and History is exercised because
        # it is the mode that fetches: a broken request, an axis built from an
        # undefined span, or a series the API won't serve all surface here and
        # nowhere else.
        _goto(page, f"{url}?tab=dashboard", problems)
        page.wait_for_timeout(1200)
        charts = page.locator("button:has-text('Chart'):visible")
        if not charts.count():
            problems.append("dashboard: no visible Chart button — UI changed?")
        for idx in range(min(charts.count(), 2)):   # AC and DC
            for attempt in (1, 2):
                page.locator("button:has-text('Chart'):visible").nth(idx).click()
                page.wait_for_timeout(700)

                hist = page.locator("button:has-text('History'):visible").first
                if not hist.count():
                    problems.append(f"chart {idx}: History mode button missing")
                else:
                    hist.click()
                    page.wait_for_timeout(1100)   # lets the fetch land
                    # Presets re-anchor the range and refetch each time.
                    for preset in ("Yesterday", "Last 24h", "Today"):
                        opt = page.locator(f"button:has-text('{preset}'):visible").first
                        if opt.count():
                            opt.click()
                            page.wait_for_timeout(700)
                    if attempt == 1 and not page.locator(
                        "input[type='datetime-local']:visible"
                    ).count():
                        problems.append(f"chart {idx}: history range inputs missing")
                    # Toggling a series in history mode must not refetch or
                    # rebuild against a stale axis.
                    series = page.locator("button:has-text('· V'):visible").first
                    if series.count():
                        series.click()
                        page.wait_for_timeout(450)
                    back = page.locator("button:has-text('Live'):visible").first
                    if back.count():
                        back.click()
                        page.wait_for_timeout(800)

                close = page.locator("button:has-text('Close'):visible").first
                if close.count():
                    close.click()
                else:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(500)

        # Dashboard → the Cards show/reorder panel. The arrows mutate the same
        # cardOrder the dashboard renders from, so a broken move shows up as a
        # card vanishing rather than a console error — assert the order really
        # changed and really came back.
        _goto(page, f"{url}?tab=dashboard", problems)
        page.wait_for_timeout(1000)
        cards_btn = page.locator("button:has-text('Cards'):visible").first
        if not cards_btn.count():
            problems.append("dashboard: Cards config button missing")
        else:
            cards_btn.click()
            page.wait_for_timeout(500)
            _ORDER_JS = (
                "() => Alpine.$data(document.querySelector("
                "'[x-data*=dashboardTab]')).cardOrder.join(',')"
            )

            def order():
                return page.evaluate(_ORDER_JS)
            before = order()
            down = page.locator("button[aria-label^='Move '][aria-label$=' down']:visible")
            if not down.count():
                problems.append("dashboard: card reorder arrows missing")
            else:
                down.first.click()
                page.wait_for_timeout(300)
                if order() == before:
                    problems.append("dashboard: moving a card down changed nothing")
                page.locator(
                    "button[aria-label^='Move '][aria-label$=' up']:visible"
                ).nth(1).click()
                page.wait_for_timeout(300)
                if order() != before:
                    problems.append("dashboard: move down then up did not round-trip")
                # The first card can't move up and the last can't move down.
                first_up = page.locator(
                    "button[aria-label^='Move '][aria-label$=' up']:visible"
                ).first
                if first_up.count() and first_up.is_enabled():
                    problems.append("dashboard: first card's up arrow is not disabled")
            cards_btn.click()
            page.wait_for_timeout(300)

        # Settings → the Point History panel that switches recording on. Read
        # only: enabling it on the live bridge is a disk-consuming change, and a
        # gate must not make one.
        _goto(page, f"{url}?tab=settings", problems)
        page.wait_for_timeout(1000)
        # Match on the modifier form actually used in the markup:
        # x-model.number, not x-model — the bare selector silently never
        # matches, which reads as "panel missing" for a panel that is there.
        if not page.locator("[x-model\\.number='ph.interval_s']").count():
            problems.append("settings: Point History panel missing")

        browser.close()

    if problems:
        print(f"CONSOLE GATE FAILED — {len(problems)} problem(s):")
        for pr in problems:
            print(f"  - {pr}")
        return 1

    print("CONSOLE GATE PASSED — no console errors, page errors or failed requests")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8100")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin")
    args = ap.parse_args()
    return run(args.url.rstrip("/"), args.user, args.password)


if __name__ == "__main__":
    sys.exit(main())
