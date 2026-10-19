"""SunSpec Explorer export (#228): all or chosen models, serial redacted by default.

Users share these publicly (an issue, a Gist), so with redaction on the serial
must not appear anywhere: not in model 1's SN, not inside another string
point, not as its 8-character short id. Everything else stays as it is.
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.support import sunspec_export as sx

SERIAL = "TESTSERIAL00000000001"
SHORT_ID = SERIAL[-8:]
HOST = "192.0.2.10"
GW_NAME = "Garage Battery"


def _rec(mid, label, name, addr, type_="uint16", unit="", access="R", plabel=""):
    return {"model_id": mid, "model_label": label, "point_name": name, "address": addr,
            "type": type_, "unit": unit, "access": access, "label": plabel,
            "scale_factor": None}


CATALOG = [
    _rec(1, "Common", "Mn", 40004, "string", plabel="Manufacturer"),
    _rec(1, "Common", "SN", 40052, "string", plabel="Serial Number"),
    _rec(1, "Common", "Opt", 40036, "string"),
    _rec(714, "DER DC Measurement", "DCW", 1048, "int16", "W"),
    _rec(714, "DER DC Measurement", "NPrt", 1046),
    _rec(701, "DER AC Measurement", "W", 900, "int16", "W"),
]
POINTS = {
    "1.Mn": "FranklinWH",
    "1.SN": SERIAL,
    "1.Opt": f"build for {SERIAL} / {SHORT_ID}",
    "714.DCW": -400,
    "n_prt": 1,  # snake_case fallback, as the Explorer resolves it
    "W": 1200,  # bare-name fallback
}
NOW = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)


def _build(**kw):
    kw.setdefault("serials", [SERIAL])
    kw.setdefault("now", NOW)
    return sx.build_export(CATALOG, POINTS, **kw)


def test_values_resolve_like_the_explorer():
    exp = _build(redact_serial=False)
    vals = {(m["model_id"], p["name"]): p["value"] for m in exp["models"] for p in m["points"]}
    assert vals[(714, "DCW")] == -400
    assert vals[(714, "NPrt")] == 1
    assert vals[(701, "W")] == 1200
    assert vals[(1, "Mn")] == "FranklinWH"


def test_all_models_by_default_in_model_order_and_address_order():
    exp = _build()
    assert [m["model_id"] for m in exp["models"]] == [1, 701, 714]
    assert [p["name"] for p in exp["models"][0]["points"]] == ["Mn", "Opt", "SN"]
    assert (exp["model_count"], exp["point_count"]) == (3, 6)


def test_chosen_models_only():
    exp = _build(model_ids=[714, 1])
    assert [m["model_id"] for m in exp["models"]] == [714, 1]


def test_unknown_model_is_an_error():
    with pytest.raises(KeyError, match="999"):
        _build(model_ids=[1, 999])


def test_serial_redacted_everywhere_by_default():
    exp = _build()
    text = sx.render_json(exp) + sx.render_csv(exp)
    assert SERIAL not in text and SHORT_ID not in text
    pts = {p["name"]: p["value"] for p in exp["models"][0]["points"]}
    assert pts["SN"] == "<serial>"
    assert pts["Opt"] == "build for <serial> / <serial>"
    assert pts["Mn"] == "FranklinWH"  # nothing else touched
    assert exp["serial_redacted"] is True


def test_serial_pattern_redacted_even_when_not_on_file():
    exp = _build(serials=[])
    assert next(p["value"] for p in exp["models"][0]["points"] if p["name"] == "SN") == "<serial>"
    assert SERIAL not in sx.render_json(exp)


def test_redaction_off_keeps_the_serial():
    exp = _build(redact_serial=False)
    assert SERIAL in sx.render_json(exp)
    assert exp["serial_redacted"] is False


def test_gateway_header_has_model_and_firmware_only():
    exp = _build(gateway={"device_type": "agate", "model": "aGate X-01",
                          "firmware": "V10R01B04D00", "host": HOST, "name": GW_NAME})
    assert exp["gateway"] == {"device_type": "agate", "model": "aGate X-01",
                              "firmware": "V10R01B04D00"}


def test_csv_has_one_row_per_point():
    rows = list(csv.reader(io.StringIO(sx.render_csv(_build()))))
    assert rows[0] == sx.CSV_HEADER
    assert len(rows) == 1 + 6
    assert rows[-1][:3] == ["714", "DER DC Measurement", "DCW"]


def test_filenames():
    assert sx.filename(_build(), "json", all_models=True) == "sunspec_all_redacted.json"
    assert sx.filename(_build(model_ids=[714]), "csv", all_models=False) == (
        "sunspec_model_714_redacted.csv")
    assert sx.filename(_build(model_ids=[1, 714], redact_serial=False), "json",
                       all_models=False) == "sunspec_models_1_714.json"


# ── REST: GET /api/models/export ───────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        await _seed()
        yield ac


async def _seed() -> None:
    db = app.state.db
    await db.execute(
        "UPDATE gateways SET host = ?, serial = ?, name = ?, model = ?, firmware = ? "
        "WHERE id = 'default'",
        (HOST, SERIAL, GW_NAME, "aGate X-01", "V10R01B04D00"),
    )
    await db.execute("DELETE FROM device_points")
    await db.execute("DELETE FROM device_models")
    models: dict[int, int] = {}
    for rec in CATALOG:
        mid = rec["model_id"]
        if mid not in models:
            cur = await db.execute(
                "INSERT INTO device_models (gateway_id, model_id, label, captured_at, hash) "
                "VALUES ('default', ?, ?, ?, 'x')",
                (mid, rec["model_label"], time.time()),
            )
            models[mid] = cur.lastrowid
        await db.execute(
            "INSERT INTO device_points (model_db_id, point_name, type, unit, addr, writable) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (models[mid], rec["point_name"], rec["type"], rec["unit"], rec["address"]),
        )
    await db.commit()


async def test_api_export_redacts_and_omits_address_and_name(client):
    r = await client.get("/api/models/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "sunspec_all_redacted.json"
    assert body["serial_redacted"] is True
    assert body["model_count"] == 3
    exp = json.loads(body["content"])
    assert exp["gateway"]["model"] == "aGate X-01"
    for secret in (SERIAL, SHORT_ID, HOST, GW_NAME):
        assert secret not in r.text


async def test_api_export_chosen_models_as_csv(client):
    r = await client.get("/api/models/export", params={"models": "714", "format": "csv"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "sunspec_model_714_redacted.csv"
    assert body["media_type"] == "text/csv"
    assert body["content"].splitlines()[1].startswith("714,")


async def test_api_export_bad_requests(client):
    assert (await client.get("/api/models/export", params={"format": "xml"})).status_code == 400
    assert (await client.get("/api/models/export", params={"models": "1,x"})).status_code == 400
    r = await client.get("/api/models/export", params={"models": "999"})
    assert r.status_code == 404 and "999" in r.text
    r = await client.get("/api/models/export", params={"gateway_id": "nope"})
    assert r.status_code == 404


def test_addresses_in_both_bases_and_the_note():
    """#273: base-0 is the PDU address the aGate answers, and FranklinWH's PICS
    numbering; base-1 is the same numbered from 1. The note says 40001+ fails."""
    exp = _build()
    dcw = next(p for m in exp["models"] for p in m["points"] if p["name"] == "DCW")
    assert (dcw["address_base0"], dcw["address_base1"]) == (1048, 1049)
    assert "address" not in dcw
    assert "base 0" in exp["addressing"] and "exception 2" in exp["addressing"]
    row = list(csv.reader(io.StringIO(sx.render_csv(exp))))[-1]
    assert row[sx.CSV_HEADER.index("Address (base-0)")] == "1048"
    assert row[sx.CSV_HEADER.index("Address (base-1)")] == "1049"
