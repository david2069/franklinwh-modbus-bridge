"""A log row's gateway comes from "Gateway <id>: …" only when <id> is one token."""

import collections
import logging

import pytest

from franklinwh_bridge.main import LogBufferHandler


@pytest.mark.parametrize(("message", "extra", "expected"), [
    ("Gateway gw1: connected", {}, "gw1"),
    ("Gateway default: external battery dispatch ended after 32s", {}, "default"),
    ("Gateway demo started (MOCK): Demo aGate", {}, ""),
    ("Gateway demo started (MOCK): Demo aGate", {"gateway_id": "demo"}, "demo"),
    ("Bridge started: v0.2.39", {}, ""),
])
def test_gateway_id(message, extra, expected):
    buf = collections.deque()
    handler = LogBufferHandler(buf)
    record = logging.LogRecord("franklinwh_bridge.x", logging.INFO, __file__, 1,
                               message, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    handler.emit(record)
    assert buf[0]["gateway_id"] == expected
