"""Sequencer import from GitHub, a URL or pasted text (#294).

An import fetches and checks; it never runs. Saving asks before replacing,
and a name can't point outside the sequences folder.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import HTTPException

from franklinwh_bridge.api import admin
from franklinwh_bridge.support import sequence_import as si

SEQ = json.dumps([
    {"step": "Enable", "writes": {"704.WSetEna": 1}, "verify": True},
    {"step": "Look", "reads": ["713.SoC"], "sleep_ms": 500},
])
USER = {"username": "admin", "role": "admin"}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_check_previews_steps_and_writes():
    p = si.check(SEQ)
    assert p["writes"] == ["704.WSetEna=1"]
    assert [s["name"] for s in p["steps"]] == ["Enable", "Look"]
    assert p["steps"][1]["reads"] == 1 and p["steps"][1]["wait"] is True


def test_check_accepts_steps_with_cleanup():
    text = json.dumps({"steps": [{"step": "a"}], "cleanup": [{"writes": {"704.WSetEna": 0}}]})
    p = si.check(text)
    assert p["steps"][1]["kind"] == "cleanup"
    assert p["writes"] == ["704.WSetEna=0"]


@pytest.mark.parametrize("text, msg", [
    ("not json", "Invalid JSON"),
    ('{"a": 1}', "Expected a list"),
    ("[]", "no steps"),
    ('[1]', "Step 1 is not an object"),
    ('[{"writes": [1]}]', '"writes" must be an object'),
    ('[{"reads": "713.SoC"}]', '"reads" must be a list'),
    ('[{"sleep_ms": "5"}]', '"sleep_ms" must be a number'),
])
def test_check_refuses_bad_sequences(text, msg):
    with pytest.raises(si.SequenceImportError, match=msg):
        si.check(text)


def test_check_refuses_oversize():
    with pytest.raises(si.SequenceImportError, match="KB"):
        si.check("[" + " " * si.MAX_BYTES + "]")


@pytest.mark.parametrize("name, ok", [
    ("charge_test-2", True), ("../x", False), ("a/b", False), ("", False), ("a" * 65, False),
])
def test_valid_name(name, ok):
    assert si.valid_name(name) is ok


def test_raw_url():
    assert si.raw_url("https://github.com/o/r/blob/v1/seq/a.json") == (
        "https://raw.githubusercontent.com/o/r/v1/seq/a.json")
    assert si.raw_url("https://example.com/a.json") == "https://example.com/a.json"
    with pytest.raises(si.SequenceImportError, match="https"):
        si.raw_url("http://example.com/a.json")
    with pytest.raises(si.SequenceImportError, match="https"):
        si.raw_url("file:///etc/passwd")


async def test_fetch_converts_and_caps():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=SEQ)

    async with _client(handler) as c:
        assert await si.fetch("https://github.com/o/r/blob/main/a.json", c) == SEQ
    assert seen == ["https://raw.githubusercontent.com/o/r/main/a.json"]

    async with _client(lambda req: httpx.Response(200, content=b"x" * (si.MAX_BYTES + 1))) as c:
        with pytest.raises(si.SequenceImportError, match="KB"):
            await si.fetch("https://example.com/a.json", c)
    async with _client(lambda req: httpx.Response(404)) as c:
        with pytest.raises(si.SequenceImportError, match="404"):
            await si.fetch("https://example.com/a.json", c)


def test_latest_tag_is_by_version_not_order():
    assert si.latest_tag(["v0.2.9", "v0.2.41", "v0.2.100", "x", "v1.0.0-rc1"]) == "v0.2.100"
    assert si.latest_tag(["nope"]) == ""


async def test_list_samples_marks_new_changed_same(tmp_path):
    (tmp_path / "same.json").write_text("A")
    (tmp_path / "changed.json").write_text("old")

    def handler(req):
        if req.url.path.endswith("/tags"):
            return httpx.Response(200, json=[{"name": "v9.9.9"}, {"name": "v10.0.0-rc1"}])
        assert req.url.params["ref"] == "v9.9.9"
        return httpx.Response(200, json=[
            {"type": "file", "name": n, "sha": sha, "download_url": f"https://raw/{n}"}
            for n, sha in [("same.json", si.git_blob_sha(b"A")),
                           ("changed.json", si.git_blob_sha(b"new")),
                           ("new.json", "x"), ("README.md", "y")]
        ])

    async with _client(handler) as c:
        out = await si.list_samples(tmp_path, c)
    assert out["tag"] == "v9.9.9"
    assert {s["name"]: s["status"] for s in out["samples"]} == {
        "same": "same", "changed": "changed", "new": "new"}


@pytest.fixture
def seq_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "SEQUENCES_DIR", tmp_path)
    return tmp_path


async def test_save_asks_before_replacing_and_logs(seq_dir, caplog):
    req = admin.SequenceImportRequest(name="imp", content=SEQ, source="https://x/a.json")
    with caplog.at_level(logging.INFO, logger=admin.logger.name):
        assert (await admin.sequence_import_save(req, USER))["ok"] is True
    assert (seq_dir / "imp.json").read_text() == SEQ
    assert "imported 'imp' from https://x/a.json by user:admin" in caplog.text

    again = await admin.sequence_import_save(req, USER)
    assert again["ok"] is False and again["exists"] is True
    req.overwrite = True
    assert (await admin.sequence_import_save(req, USER))["replaced"] is True


async def test_save_refuses_bad_name_or_content(seq_dir):
    bad = admin.SequenceImportRequest(name="../evil", content=SEQ)
    assert (await admin.sequence_import_save(bad, USER))["ok"] is False
    bad = admin.SequenceImportRequest(name="ok", content="[1]")
    assert (await admin.sequence_import_save(bad, USER))["ok"] is False
    assert list(seq_dir.iterdir()) == []


async def test_preview_runs_nothing(seq_dir):
    out = await admin.sequence_preview(admin.SequencePreviewRequest(content=SEQ), USER)
    assert out["ok"] is True and out["writes"] == ["704.WSetEna=1"]
    assert list(seq_dir.iterdir()) == []


async def test_existing_routes_check_names(seq_dir):
    put = await admin.save_sequence("a.b", admin.SequenceSaveRequest(content=SEQ))
    assert put["ok"] is False
    for fn in (admin.get_sequence, admin.delete_sequence):
        with pytest.raises(HTTPException) as e:
            await fn("..")
        assert e.value.status_code == 400
    (seq_dir / "x.json").write_text(SEQ)
    out = await admin.rename_sequence("x", admin.SequenceRenameRequest(new_name="../y"))
    assert out["ok"] is False and (seq_dir / "x.json").exists()


BUNDLED = sorted(si.Path(admin.__file__).parent.parent.glob("sequences/*.json"))


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem)
def test_bundled_samples_pass_the_import_check(path):
    si.check(path.read_text())
