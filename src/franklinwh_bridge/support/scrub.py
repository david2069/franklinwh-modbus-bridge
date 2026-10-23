"""Remove personal and identifying details from free text (log messages).

The support bundle builds its structured fields from an allowlist, so they
never carry a host or a serial in the first place. Log messages are the one
place free text gets in, and a message can say anything: "connect to
192.0.2.10:502 failed", "serial TESTSERIAL00000000001", a broker URL. Those go
through :func:`scrub` before they are shown.

Two passes:

1. **Known values** — the exact hosts, serials, names, URLs and usernames this
   install has on file. Exact matching catches what no pattern can guess (a
   gateway called "Garage" is just a word).
2. **Patterns** — IPs, URLs, MACs, emails, serial-like and token-like strings,
   coordinates and home-directory paths, for anything not on file.

Over-redaction is the safe failure: a number that looks like a coordinate is
replaced. Ordinary readings ("5000 W"), times ("17:30:00") and versions
("0.2.3") are left alone.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping

# Placeholders. Typed, so a reader can still follow the message.
IP = "<ip>"
URL = "<url>"
HOST = "<host>"
MAC = "<mac>"
EMAIL = "<email>"
SERIAL = "<serial>"
TOKEN = "<token>"
COORD = "<coord>"
PATH = "<path>"
ID = "<id>"
REDACTED = "<redacted>"

_NOT_ALNUM_BEFORE = r"(?<![A-Za-z0-9])"
_NOT_ALNUM_AFTER = r"(?![A-Za-z0-9])"

_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>)\]}]+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
#: ``token=…``, ``password: …``, ``Bearer …`` — keep the key, drop the value.
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"
    r"authorization|bearer|client[_-]?id|session(?:[_-]?id)?|username|user)\b"
    r"(['\"]?\s*[=:]\s*['\"]?)"
    r"(?!<)([^\s'\",;)}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)(?!<)[A-Za-z0-9._~+/=-]{8,}")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_MAC_RE = re.compile(
    _NOT_ALNUM_BEFORE + r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}" + _NOT_ALNUM_AFTER
)
#: IPv6 candidates (validated with ``ipaddress``, so "17:30:00" is not one).
_IPV6_CAND_RE = re.compile(r"(?<![\w:.])\[?([0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*)(%[\w.]+)?\]?(?![\w:])")
_IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?::(\d{1,5}))?(?![\d.]*\d)")
_PATH_RE = re.compile(
    r"(?:/Users|/home|/root|/var/folders|/private/var)/[^\s'\":,;)]+"
    r"|[A-Za-z]:\\Users\\[^\s'\":,;)]+"
)
#: Host names, judged by their last label. Module names such as
#: ``franklinwh_bridge.modbus.poller`` end in labels that are not here.
_TLDS = (
    "local|lan|home|internal|localdomain|intranet|corp|example|test|invalid|"
    "arpa|com|net|org|io|dev|app|cloud|au|nz|uk|us|ca|de|fr|nl|eu|info|biz|me"
)
_HOST_RE = re.compile(
    _NOT_ALNUM_BEFORE
    + r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:"
    + _TLDS
    + r")"
    + r"(?::\d{1,5})?"
    + r"(?![A-Za-z0-9_])"
)
#: ``host=nas``: a single-label host named by its key.
_HOST_KV_RE = re.compile(
    r"(?i)\b(host|hostname|server|broker|addr|address)(['\"]?\s*[=:]\s*['\"]?)(?!<)([A-Za-z0-9._-]+)"
)
#: Serial-like: 12+ upper-case letters AND digits (e.g. 10000000A00X00000001).
#: Firmware versions look the same (V10R01B04D00) and are not identifying.
_SERIAL_RE = re.compile(
    _NOT_ALNUM_BEFORE
    + r"(?!V\d{1,3}R\d)"
    + r"(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12,}"
    + _NOT_ALNUM_AFTER
)
_LONG_HEX_RE = re.compile(_NOT_ALNUM_BEFORE + r"[0-9a-fA-F]{16,}" + _NOT_ALNUM_AFTER)
#: base64 / url-safe tokens: 24+ chars mixing letters and digits.
#: Mixed case is required, so lower-case MQTT topics and module paths
#: (``franklinwh_00000001/soc/state``) are not mistaken for one.
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+/_-])"
    r"(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])"
    r"[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_-])"
)
_COORD_KV_RE = re.compile(
    r"(?i)\b(lat|lon|lng|latitude|longitude)(['\"]?\s*[=:]\s*['\"]?)-?\d+(?:\.\d+)?"
)
#: "lat, lon" / "lat/lon" pairs, each with 2+ decimals.
_COORD_PAIR_RE = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{2,}\s*[,/]\s*-?\d{1,3}\.\d{2,}(?![\d.])")
#: A lone decimal with 4+ places — the precision of a street address.
_COORD_RE = re.compile(r"(?<![\d.])-?[1-9]\d{0,2}\.\d{4,}(?![\d.])")
#: 8-char hex ids (serial tails in MQTT topics, the client id suffix). Must
#: contain a digit, so ordinary words made of a-f ("defaced") survive.
_SHORT_ID_RE = re.compile(
    _NOT_ALNUM_BEFORE + r"(?=[0-9A-Fa-f]*\d)[0-9A-Fa-f]{8}" + _NOT_ALNUM_AFTER
)


def _ip_sub(m: re.Match) -> str:
    try:
        addr = ipaddress.ip_address(m.group(1))
    except ValueError:
        return m.group(0)
    if addr.is_loopback or addr.is_unspecified:
        return m.group(0)  # 127.0.0.1 says "misconfigured", not "where you live"
    return IP + (":<port>" if m.group(2) else "")


def _ipv6_sub(m: re.Match) -> str:
    cand = m.group(1)
    if cand.count(":") < 2:
        return m.group(0)
    try:
        addr = ipaddress.ip_address(cand)
    except ValueError:
        return m.group(0)
    if addr.is_loopback or addr.is_unspecified:
        return m.group(0)
    return IP


def compile_known(known: Mapping[str, str] | None) -> list[tuple[re.Pattern, str]]:
    """Exact-value replacements, longest first (so a URL beats its host)."""
    out: list[tuple[re.Pattern, str]] = []
    for value, placeholder in sorted(
        (known or {}).items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        v = (value or "").strip()
        if len(v) < 3:
            continue
        out.append(
            (
                re.compile(_NOT_ALNUM_BEFORE + re.escape(v) + _NOT_ALNUM_AFTER, re.IGNORECASE),
                placeholder,
            )
        )
    return out


def scrub(text: str | None, known: Mapping[str, str] | None = None) -> str:
    """Return ``text`` with identifying details replaced by placeholders.

    ``known`` maps exact values (hosts, serials, names, URLs) to the
    placeholder each should become; it is applied before the patterns.
    """
    if not text:
        return ""
    s = str(text)
    for pat, placeholder in compile_known(known):
        s = pat.sub(placeholder, s)
    return scrub_patterns(s)


def scrub_patterns(s: str) -> str:
    """The pattern pass of :func:`scrub` (no known values)."""
    s = _URL_RE.sub(URL, s)
    s = _EMAIL_RE.sub(EMAIL, s)
    s = _JWT_RE.sub(TOKEN, s)
    s = _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, s)
    s = _BEARER_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, s)
    s = _UUID_RE.sub(ID, s)
    s = _MAC_RE.sub(MAC, s)
    s = _IPV4_RE.sub(_ip_sub, s)
    s = _IPV6_CAND_RE.sub(_ipv6_sub, s)
    s = _PATH_RE.sub(PATH, s)
    s = _HOST_KV_RE.sub(
        lambda m: (
            m.group(0)
            if m.group(3).lower() in ("localhost", "127.0.0.1")
            else m.group(1) + m.group(2) + HOST
        ),
        s,
    )
    s = _HOST_RE.sub(HOST, s)
    s = _SERIAL_RE.sub(SERIAL, s)
    s = _LONG_HEX_RE.sub(TOKEN, s)
    s = _TOKEN_RE.sub(TOKEN, s)
    s = _COORD_KV_RE.sub(lambda m: m.group(1) + m.group(2) + COORD, s)
    s = _COORD_PAIR_RE.sub(COORD, s)
    s = _COORD_RE.sub(COORD, s)
    s = _SHORT_ID_RE.sub(ID, s)
    return s


def scrub_serial_patterns(s: str) -> str:
    """Only the serial-number pattern of :func:`scrub_patterns`.

    For text that is shared on purpose (a SunSpec export) where everything
    but the serial should stay as it is.
    """
    return _SERIAL_RE.sub(SERIAL, s)


def scrub_ip_patterns(s: str) -> str:
    """Only the IPv4 and IPv6 patterns of :func:`scrub_patterns` (loopback kept)."""
    s = _IPV4_RE.sub(_ip_sub, s)
    return _IPV6_CAND_RE.sub(_ipv6_sub, s)
