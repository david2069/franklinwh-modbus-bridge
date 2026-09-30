"""Acknowledging the legal notice.

Logging the notice at startup proves it was emitted, not that anybody read it —
a bridge someone else installed can run for months without its operator opening
a console. These cover the part that actually puts the words in front of a
person, and the record that it happened.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge import disclaimer
from franklinwh_bridge.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


# ── What the modal is handed ─────────────────────────────────


async def test_a_fresh_user_has_not_acknowledged(client):
    body = (await client.get("/api/disclaimer")).json()

    assert body["acknowledged"] is False


async def test_the_text_comes_from_the_server(client):
    """So the words shown and the version they're recorded against can't drift —
    the UI never keeps its own copy of the wording."""
    body = (await client.get("/api/disclaimer")).json()

    assert body["paragraphs"] == list(disclaimer.MODAL_PARAGRAPHS)
    assert body["version"] == disclaimer.VERSION


async def test_it_carries_the_links_the_modal_offers(client):
    body = (await client.get("/api/disclaimer")).json()

    assert body["issues_url"] == disclaimer.ISSUES_URL
    assert body["docs_url"]


async def test_the_paragraphs_say_the_things_that_matter(client):
    joined = " ".join((await client.get("/api/disclaimer")).json()["paragraphs"]).lower()

    assert "unofficial" in joined
    assert "as-is" in joined or "as is" in joined
    assert "do not contact franklinwh support" in joined


async def test_it_covers_compliance_and_anti_circumvention(client):
    """This bridge is the write plane — it sets mode, reserve and power, and can
    schedule those unattended. A user must be told not to drive it through their
    grid profile, export limit or VPP programme conditions."""
    joined = " ".join((await client.get("/api/disclaimer")).json()["paragraphs"]).lower()

    assert "compliant" in joined
    assert "bypass" in joined
    assert "vpp" in joined or "programme" in joined


async def test_it_does_not_claim_to_use_apis_it_never_touches(client):
    """The sibling projects' wording names FranklinWH's Direct Connect and cloud
    APIs. This bridge speaks Modbus only, and copying that text verbatim would
    have the notice assert something untrue about what it connects to."""
    joined = " ".join((await client.get("/api/disclaimer")).json()["paragraphs"]).lower()

    assert "direct connect" not in joined
    assert "cloud api" not in joined
    assert "modbus" in joined


async def test_the_modal_links_to_the_authoritative_terms(client):
    """The modal is a summary; the binding text is in LICENSE, so it has to be
    reachable from the dialog rather than only from the repo root."""
    body = (await client.get("/api/disclaimer")).json()

    assert body["terms_url"] == disclaimer.TERMS_URL
    assert body["terms_url"].endswith("LICENSE")


# ── Accepting ────────────────────────────────────────────────


async def test_accepting_is_remembered(client):
    await client.post("/api/disclaimer", json={"agreed": True})

    assert (await client.get("/api/disclaimer")).json()["acknowledged"] is True


async def test_accepting_twice_records_one_acknowledgement(client):
    """Someone who clears their browser and sees the modal again has not
    consented twice; duplicate rows would make the trail read as though they
    had."""
    await client.post("/api/disclaimer", json={"agreed": True})
    await client.post("/api/disclaimer", json={"agreed": True})

    acks = (await client.get("/api/disclaimer/acks")).json()["acks"]
    assert len(acks) == 1


async def test_the_acknowledgement_is_recorded_with_who_and_when(client):
    """The whole value of an acknowledgement after the fact."""
    await client.post("/api/disclaimer", json={"agreed": True})

    ack = (await client.get("/api/disclaimer/acks")).json()["acks"][0]
    assert ack["user_id"]
    assert ack["acked_at"] > 0
    assert ack["version"] == disclaimer.VERSION


# ── Declining ────────────────────────────────────────────────


async def test_declining_is_not_stored(client):
    """Continue without ticking is a real answer. It must not be recorded as
    consent, and the notice must come back."""
    resp = await client.post("/api/disclaimer", json={"agreed": False})

    assert resp.json()["acknowledged"] is False
    assert (await client.get("/api/disclaimer")).json()["acknowledged"] is False


async def test_declining_leaves_no_trail_row(client):
    await client.post("/api/disclaimer", json={"agreed": False})

    assert (await client.get("/api/disclaimer/acks")).json()["acks"] == []


async def test_it_keeps_prompting_until_accepted(client):
    """The behaviour asked for: decline, decline, then accept."""
    for _ in range(3):
        await client.post("/api/disclaimer", json={"agreed": False})
        assert (await client.get("/api/disclaimer")).json()["acknowledged"] is False

    await client.post("/api/disclaimer", json={"agreed": True})

    assert (await client.get("/api/disclaimer")).json()["acknowledged"] is True


# ── Versioning ───────────────────────────────────────────────


async def test_a_new_notice_version_prompts_again(client, monkeypatch):
    """Consent to wording somebody never saw is not consent."""
    await client.post("/api/disclaimer", json={"agreed": True})
    assert (await client.get("/api/disclaimer")).json()["acknowledged"] is True

    monkeypatch.setattr(disclaimer, "VERSION", "99-changed-terms")

    assert (await client.get("/api/disclaimer")).json()["acknowledged"] is False


async def test_accepting_a_new_version_does_not_erase_the_old_record(client, monkeypatch):
    """An acknowledgement is a record of something that happened; a later one
    does not make the earlier one untrue."""
    original = disclaimer.VERSION
    await client.post("/api/disclaimer", json={"agreed": True})
    monkeypatch.setattr(disclaimer, "VERSION", "99-changed-terms")
    await client.post("/api/disclaimer", json={"agreed": True})

    versions = {a["version"] for a in (await client.get("/api/disclaimer/acks")).json()["acks"]}
    assert versions == {original, "99-changed-terms"}


# ── It is persisted, not browser state ───────────────────────


async def test_it_survives_a_new_client(client, tmp_path, monkeypatch):
    """The point of storing it server-side: a different browser is still the
    same person, and the record outlives any localStorage."""
    await client.post("/api/disclaimer", json={"agreed": True})

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://other") as fresh:
        assert (await fresh.get("/api/disclaimer")).json()["acknowledged"] is True


async def test_acceptance_is_written_to_the_persisted_log(client):
    """An acknowledgement that lives only in a ring buffer quietly disappears."""
    await client.post("/api/disclaimer", json={"agreed": True})

    db = app.state.db
    async with db.execute(
        "SELECT detail FROM startup_log WHERE event = 'disclaimer_ack'"
    ) as cur:
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert disclaimer.VERSION in rows[0][0]
