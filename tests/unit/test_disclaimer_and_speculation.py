"""The legal notice, and not repeating the library's SPAN-lock guess.

Two unrelated-looking things that are the same concern: what this software
asserts to the person running it. One is the notice it owes them up front; the
other is an explanation it was passing on without evidence.
"""

from __future__ import annotations

import pathlib

import pytest

from franklinwh_bridge import disclaimer
from franklinwh_bridge.publish.command_handler import (
    _SPECULATION,
    _strip_speculation,
)

# ── The notice ────────────────────────────────────────────────


def test_it_says_it_is_unofficial():
    assert "unofficial" in disclaimer.FULL.lower()
    assert "not endorsed" in disclaimer.FULL.lower()


def test_it_disclaims_warranty():
    assert '"AS IS"' in disclaimer.FULL
    assert "fitness for any particular purpose" in disclaimer.FULL


def test_it_tells_people_not_to_call_the_vendor():
    """The addition to the cloud-docs wording. A defect routed to FranklinWH
    support wastes their time and does not get the bug fixed."""
    assert "DO NOT CONTACT FRANKLINWH SUPPORT" in disclaimer.FULL


def test_it_gives_somewhere_to_report_instead():
    """Telling someone where not to go is only useful with a destination."""
    assert disclaimer.ISSUES_URL in disclaimer.FULL
    assert disclaimer.ISSUES_URL in disclaimer.SHORT
    assert disclaimer.ISSUES_URL in disclaimer.MARKDOWN


def test_it_warns_that_this_writes_to_battery_hardware():
    assert "WRITE commands" in disclaimer.FULL


def test_the_short_form_still_carries_the_three_essentials():
    """It is what lands in a log line, where nobody will click through."""
    low = disclaimer.SHORT.lower()
    assert "unofficial" in low
    assert "as is" in low
    assert "do not contact franklinwh support" in low


def test_the_banner_is_framed_for_a_log():
    banner = disclaimer.banner()
    assert disclaimer.FULL in banner
    assert "=" * 72 in banner


def test_the_api_description_carries_it():
    """So /docs and any generated client show the notice, not just the README."""
    from franklinwh_bridge.main import app

    assert app.description
    assert "Unofficial software" in app.description
    assert disclaimer.ISSUES_URL in app.description


# ── Not repeating an unverified cause ─────────────────────────

_LIB_MESSAGE = (
    "Write failed (Read-Only?): Hardware ignored write to Emergency Backup. "
    "Register stayed at Self-Consumption. "
    "Ensure 'SPAN Modbus' is unlocked in installer settings."
)


def test_the_span_guess_is_removed():
    """It sends the user after a fix never shown to exist."""
    out = _strip_speculation(_LIB_MESSAGE)

    assert _SPECULATION not in out
    assert "SPAN" not in out


def test_what_actually_happened_is_kept():
    """Stripping must not cost the user the facts — which register, which value."""
    out = _strip_speculation(_LIB_MESSAGE)

    assert "Hardware ignored write to Emergency Backup" in out
    assert "Register stayed at Self-Consumption" in out


def test_the_known_position_replaces_the_guess():
    out = _strip_speculation(_LIB_MESSAGE)

    assert "the cause is not established" in out
    assert "vendor-issues.md" in out


def test_a_message_without_the_sentence_is_untouched():
    """Written as a strip, not a rewrite, so it becomes a no-op the day the
    library drops the sentence rather than needing removal in lockstep."""
    clean = "Native mode changed to Self-Consumption"

    assert _strip_speculation(clean) == clean


@pytest.mark.parametrize("value", ["", None])
def test_empty_results_survive(value):
    assert _strip_speculation(value) == value


def test_the_reserve_variant_is_covered_too():
    """The same sentence is appended to reserve writes, not only mode writes."""
    msg = (
        "Write failed (Read-Only?): Hardware ignored write to TOU reserve 50%. "
        "Register stayed at 5%. "
        "Ensure 'SPAN Modbus' is unlocked in installer settings."
    )

    out = _strip_speculation(msg)

    assert "SPAN" not in out
    assert "TOU reserve 50%" in out


def test_no_double_space_where_the_sentence_was_cut():
    out = _strip_speculation(_LIB_MESSAGE)

    assert "  " not in out


def test_the_conformance_documents_are_cited_not_just_the_vendor_page():
    """A link to a members listing makes the reader hunt for the document that
    actually settles the question; this IS the Modbus project, so the PICS is
    the spec it implements."""
    assert disclaimer.PICS_DOC_ID == "SM-000028"
    assert disclaimer.PICS_URL.endswith(".xlsx")
    assert disclaimer.IEEE_1547_URL.endswith(".pdf")
    assert disclaimer.PICS_DOC_ID in disclaimer.PICS_URL
    assert disclaimer.PICS_DOC_ID in disclaimer.IEEE_1547_URL


def test_the_full_notice_carries_compliance_and_the_pics():
    assert "COMPLIANCE" in disclaimer.FULL
    assert "bypass" in disclaimer.FULL
    assert disclaimer.PICS_URL in disclaimer.FULL
    assert disclaimer.TERMS_URL in disclaimer.FULL


def test_the_api_description_carries_compliance_too():
    """All three surfaces state the same terms; one lagging is how they drift."""
    assert "Compliance" in disclaimer.MARKDOWN
    assert disclaimer.PICS_URL in disclaimer.MARKDOWN


def test_the_licence_states_the_notices_add_no_restrictions():
    """They allocate risk and state facts. If they restricted use, the package
    metadata declaring MIT would be wrong — see pyproject."""
    licence = pathlib.Path("LICENSE").read_text()

    assert "ADDITIONAL NOTICES" in licence
    assert "do NOT add restrictions to the MIT" in licence
    assert "COMPLIANCE & ANTI-CIRCUMVENTION" in licence
    assert "SM-000028" in licence


def test_the_licence_does_not_smuggle_in_a_non_commercial_clause():
    """MIT grants commercial use. A non-commercial restriction here would make
    the declared licence inaccurate on PyPI."""
    licence = pathlib.Path("LICENSE").read_text().lower()

    assert "non-commercial" not in licence
    assert "not-for-profit" not in licence
