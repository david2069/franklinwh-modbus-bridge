"""The support-bundle scrubber: identifying details out, ordinary text intact.

Every value here is fictitious (TEST-NET addresses, example.* hosts,
TESTSERIAL… serials, 12.34/56.78).
"""

from __future__ import annotations

import pytest

from franklinwh_bridge.support.scrub import scrub


@pytest.mark.parametrize(
    "text, gone, placeholder",
    [
        ("connect to 192.0.2.10:502 failed", "192.0.2.10", "<ip>:<port>"),
        ("Connect call failed ('198.51.100.7', 502)", "198.51.100.7", "<ip>"),
        ("peer 2001:db8::1 down", "2001:db8::1", "<ip>"),
        ("link fe80::1ff:fe23:4567:890a%eth0 up", "fe80::1ff:fe23:4567:890a", "<ip>"),
        ("GET http://ha.example:8123/api/states 401", "ha.example", "<url>"),
        ("broker mqtt://u:p@broker.example:1883 refused", "broker.example", "<url>"),
        ("resolved agate.lan in 3 ms", "agate.lan", "<host>"),
        ("broker=nas01 unreachable", "nas01", "host"),  # key kept, value gone
        ("seen aa:bb:cc:dd:ee:ff on LAN", "aa:bb:cc:dd:ee:ff", "<mac>"),
        ("seen AA-BB-CC-DD-EE-01 on LAN", "AA-BB-CC-DD-EE-01", "<mac>"),
        ("mail someone@example.com", "someone@example.com", "<email>"),
        ("aGate TESTSERIAL00000000001 online", "TESTSERIAL00000000001", "<serial>"),
        ("aGate 10000000A00X00000009 online", "10000000A00X00000009", "<serial>"),
        ("topic franklinwh_00000001/soc/state", "00000001", "franklinwh_<id>/soc/state"),
        ("client franklinwh_bridge_1a2b3c4d dropped", "1a2b3c4d", "franklinwh_bridge_<id>"),
        ("token=abcdef0123456789abcdef rejected", "abcdef0123456789abcdef", "token=<redacted>"),
        ("password: hunter2isnotsafe", "hunter2isnotsafe", "password: <redacted>"),
        ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "eyJhbGci", "Bearer"),
        ("key 0123456789abcdef0123 stored", "0123456789abcdef0123", "<token>"),
        ("blob dGhpc0lzQVZlcnlMb25nU2VjcmV0VG9rZW4xMjM0NQ== x", "dGhpc0lz", "<token>"),
        ("session 6f1c2a9e-1b2c-4d3e-9f00-123456789abc ended", "6f1c2a9e", "<id>"),
        ("forecast for 12.34, 56.78 ok", "12.34", "<coord>"),
        ("forecast for 12.34/56.78 ok", "56.78", "<coord>"),
        ("site at -12.34567 56.78901", "12.34567", "<coord>"),
        ("lat=12.34 lon=56.78", "56.78", "lat=<coord>"),
        ("open /Users/someone/dev/app.py failed", "someone", "<path>"),
        ("open /home/someone/.config failed", "someone", "<path>"),
        (r"open C:\Users\someone\x.txt failed", "someone", "<path>"),
    ],
)
def test_identifying_values_are_replaced(text, gone, placeholder):
    out = scrub(text)
    assert gone not in out, out
    assert placeholder in out, out


@pytest.mark.parametrize(
    "text",
    [
        "Battery 5000 W, SoC 45.6%, -12.5 A, 3.14 kWh",
        "FranklinWH Modbus Bridge v0.2.3 starting (franklinwh-modbus 0.9.5)",
        "schedule fires at 17:30:00 on 2026-10-15",
        "Model 701 and 714 read, 1234 polls, timeout 10.0s",
        "franklinwh_bridge.modbus.poller: Gateway default: read failed",
        "config.yaml loaded; token expired for user role",
        "connect to 127.0.0.1:1883 refused",  # loopback is a diagnosis, not PII
        "topic franklinwh/default/soc/state",
        "defaced cafe",
        "firmware V10R01B04D00 / IBG V12R02B00D01",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert scrub(text) == text


def test_known_values_are_replaced_first():
    known = {
        "Garage Battery": "<gateway 1>",
        "TESTSERIAL00000000001": "<serial>",
        "http://ha.example:8123": "<url>",
        "jdoe": "<user>",
    }
    out = scrub("Garage Battery (TESTSERIAL00000000001) via http://ha.example:8123 by jdoe", known)
    assert out == "<gateway 1> (<serial>) via <url> by <user>"


def test_known_values_match_whole_words_and_ignore_case():
    out = scrub(
        "garage battery offline; jdoesmith is fine", {"Garage Battery": "<gw>", "jdoe": "<u>"}
    )
    assert out == "<gw> offline; jdoesmith is fine"


def test_short_known_values_are_ignored():
    # Two characters would match far too much ordinary text.
    assert scrub("go on", {"on": "<x>"}) == "go on"


def test_empty_input():
    assert scrub(None) == ""
    assert scrub("") == ""
