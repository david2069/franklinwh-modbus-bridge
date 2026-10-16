"""Capture the setup wizard's screens from a throwaway bridge (http://127.0.0.1:8100, admin/admin).

Usage: python docs/images/wizard/_capture.py   (opens "Run setup again"; creates nothing)
"""
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8100"
OUT = Path(__file__).resolve().parent


def shot(page, name):
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("shot", name)


def dialog(page):
    return page.locator("[role=dialog]:visible, .modal:visible, dialog[open]").last


def click(page, text):
    loc = dialog(page).locator("button:visible", has_text=text)
    loc.first.click()
    time.sleep(1.5)


with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    page = b.new_page(viewport={"width": 1280, "height": 900})
    page.goto(URL + "/login")
    page.fill("input[name=username]", "admin")
    page.fill("input[type=password]", "admin")
    page.click("button[type=submit]")
    time.sleep(3)
    if page.locator("text=Unofficial software").count():
        page.get_by_role("checkbox").first.check()
        page.get_by_role("button", name="Continue").click()
        time.sleep(1.5)
    page.goto(URL + "/?tab=settings&section=gateways")
    time.sleep(3)
    page.locator("button:visible", has_text="Run setup again").first.click()
    time.sleep(2)
    shot(page, "wizard-start")
    click(page, "Connect my FranklinWH aGate")
    shot(page, "wizard-before-you-start")
    click(page, "Next")
    shot(page, "wizard-search")
    click(page, "Enter address")
    shot(page, "wizard-enter-address")
    click(page, "Back")
    click(page, "Back")
    click(page, "Explore with a demo gateway")
    shot(page, "wizard-demo-explained")
    click(page, "Next")
    shot(page, "wizard-demo-create")
    page.keyboard.press("Escape")
    b.close()
