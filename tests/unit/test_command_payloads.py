"""Command payload coercion.

Found on real hardware. Half the control slugs carry a number (watts, seconds,
percent) and half carry an action string, but ``value`` was typed str-only. A
client that sent ``{"value": 2000}`` for a watts field got a 422 while the
*action* command right after it succeeded — arming a dispatch at whatever power
was left over from last time. The admin tab only escaped this by calling
String(value) at every call site, which is not something an API should require.
"""

from __future__ import annotations

import pytest

from franklinwh_bridge.api.gateways_api import GatewayCommandRequest
from franklinwh_bridge.api.ui import CommandRequest

MODELS = [GatewayCommandRequest, CommandRequest]


@pytest.mark.parametrize("model", MODELS)
def test_an_integer_is_accepted(model):
    """2000 watts, as the mobile dispatch card sends it."""
    assert model(slug="battery_command_power", value=2000).value == "2000"


@pytest.mark.parametrize("model", MODELS)
def test_a_float_is_accepted(model):
    assert model(slug="battery_command_power", value=1500.5).value == "1500.5"


@pytest.mark.parametrize("model", MODELS)
def test_zero_survives_as_zero(model):
    """0 disables the target-SoC exit. If it coerced to '' or vanished, a stale
    target from an earlier run would stay armed and end the next dispatch."""
    assert model(slug="battery_command_target_soc", value=0).value == "0"


@pytest.mark.parametrize("model", MODELS)
def test_action_strings_are_untouched(model):
    assert model(slug="battery_command", value="Force Charge").value == "Force Charge"


@pytest.mark.parametrize("model", MODELS)
def test_a_bool_does_not_become_python_repr(model):
    """True would otherwise stringify to 'True', which no handler parses."""
    assert model(slug="some_switch", value=True).value == "true"


@pytest.mark.parametrize("model", MODELS)
def test_the_handler_can_still_parse_what_we_produce(model):
    """The coerced form must survive the handler's own int(float(payload)).

    This is the actual contract — the schema is only correct if what it emits
    is what the command handler already knows how to read.
    """
    for raw in (2000, 1500.5, 0, "300"):
        assert int(float(model(slug="battery_command_power", value=raw).value)) is not None
