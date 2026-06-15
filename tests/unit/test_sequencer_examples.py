"""Post-implement verification for the updated franklinwh-modbus sequencer.

The bridge forwards sequence steps verbatim to ``SunSpecSequencer.run_sequence``,
so these tests verify (a) the new raw/extension + sunspec example sequences are
well-formed against the library's actual resolution contract, and (b) — when the
upgraded library is installed — that the inline reads actually resolve.

The library is read-only here and the running container is still on 0.9.0, so
the live-resolution tests SKIP until the upgraded library (EXTENSION_REGISTRY +
inline overrides) is present, then activate automatically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SEQ_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "franklinwh_bridge" / "sequences"
)
RAW_EXAMPLE = SEQ_DIR / "raw_extension_probe.json"
SUNSPEC_EXAMPLE = SEQ_DIR / "sunspec_model_probe.json"

# Feature gate: the upgraded library ships EXTENSION_REGISTRY + dict-tag support.
try:
    from franklinwh_modbus.constants import EXTENSION_REGISTRY

    HAS_REGISTRY = True
except Exception:  # noqa: BLE001 — any import failure means the old lib
    EXTENSION_REGISTRY = {}
    HAS_REGISTRY = False

requires_new_lib = pytest.mark.skipif(
    not HAS_REGISTRY,
    reason="requires franklinwh-modbus >= 0.9.x (EXTENSION_REGISTRY + inline overrides)",
)


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def _read_tags(steps: list[dict]) -> list:
    tags = []
    for step in steps:
        tags.extend(step.get("reads", []))
    return tags


# ── Structural validation (always runs) ─────────────────────────

@pytest.mark.parametrize("path", [RAW_EXAMPLE, SUNSPEC_EXAMPLE])
def test_example_is_valid_sequence(path):
    assert path.exists(), f"missing example {path.name}"
    steps = _load(path)
    assert isinstance(steps, list) and steps
    for step in steps:
        assert isinstance(step, dict)
        assert "note" in step, f"step missing note: {step}"
        # A step must do something: read, write, or sleep.
        assert any(k in step for k in ("reads", "writes", "sleep_ms")), step
        if "reads" in step:
            assert isinstance(step["reads"], list)
        if "writes" in step:
            assert isinstance(step["writes"], dict)


def test_inline_reads_use_a_valid_address_key():
    """An inline dict read must carry the address under a key the engine
    resolves: 'point', 'addr', or 'address' (resolution order point -> addr ->
    address, since franklinwh-modbus eb69825). Our examples use 'addr'."""
    valid = {"point", "addr", "address"}
    for tag in _read_tags(_load(RAW_EXAMPLE)):
        if isinstance(tag, dict):
            assert valid & tag.keys(), (
                f"inline read {tag} must carry the address under one of {valid}"
            )


def test_inline_write_override_shape():
    """Inline write overrides are {value, type, sf} dicts; plain writes are ints."""
    for step in _load(RAW_EXAMPLE):
        for _addr, val in step.get("writes", {}).items():
            if isinstance(val, dict):
                assert "value" in val, f"write override missing 'value': {val}"
            else:
                assert isinstance(val, int)


def test_all_bundled_sequences_are_valid_json():
    """Every shipped sequence parses and is a non-empty list of step dicts."""
    for path in SEQ_DIR.glob("*.json"):
        steps = json.loads(path.read_text())
        assert isinstance(steps, list), f"{path.name} is not a list"
        for step in steps:
            assert isinstance(step, dict), f"{path.name} has a non-dict step"


# ── Live library resolution (activates on the upgraded lib) ──────

@requires_new_lib
def test_example_addresses_present_in_registry():
    for addr in (15506, 15507, 15508, 15509, 15510, 15512, 16000):
        assert addr in EXTENSION_REGISTRY, f"{addr} not in EXTENSION_REGISTRY"
    assert EXTENSION_REGISTRY[15510]["type"] == "uint32"
    assert EXTENSION_REGISTRY[15512]["type"] == "uint32"
    assert "symbols" in EXTENSION_REGISTRY[15507], "15507 OnGridMode should map enums"


@requires_new_lib
def test_raw_and_inline_reads_resolve_via_get_point():
    """Raw + inline-dict reads resolve to (None, int_addr) without a device —
    get_point only touches device.models for Model.Point tags."""
    from franklinwh_modbus.sequencer import SunSpecSequencer

    seq = SunSpecSequencer(object())  # raw/dict reads never dereference the device
    for tag in _read_tags(_load(RAW_EXAMPLE)):
        model, addr = seq.get_point(tag)
        assert model is None, f"{tag} should be a raw register"
        assert isinstance(addr, int), f"{tag} did not resolve to an int address"
