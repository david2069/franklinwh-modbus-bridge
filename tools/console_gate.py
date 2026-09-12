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


def run(url: str, user: str, password: str) -> int:
    problems: list[str] = []

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
        page.on("requestfailed", lambda r: (
            problems.append(f"requestfailed: {r.url} ({r.failure})")
            if not _should_ignore(r.url) else None
        ))
        # A 404 is a completed response, not a "failed request" — without this
        # the console shows a bare "404" with no way to tell WHICH resource.
        page.on("response", lambda r: (
            problems.append(f"http {r.status}: {r.url}")
            if r.status >= 400 and not _should_ignore(r.url) else None
        ))

        page.goto(url, wait_until="networkidle")

        # Log in if we landed on the login page.
        if page.locator("input[type=password]").count():
            page.fill("input[name=username], input[type=text]", user)
            page.fill("input[type=password]", password)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

        for tab in ("dashboard", "schedule", "settings", "logs", "events"):
            page.goto(f"{url}?tab={tab}", wait_until="networkidle")
            page.wait_for_timeout(900)

        # Settings → open the service editor twice. The second open is the one
        # that catches teardown bugs.
        page.goto(f"{url}?tab=settings", wait_until="networkidle")
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
