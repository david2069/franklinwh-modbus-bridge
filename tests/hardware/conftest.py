"""Hardware test fixtures — require a real aGate on the network.

Environment variables:
    FRANKLINWH_TEST_HOST  aGate IP (default: 192.168.1.100)
    FRANKLINWH_TEST_PORT  Modbus port (default: 502)
    FRANKLINWH_TEST_UNIT  Unit ID (default: 1)
"""

import os

import pytest
from franklinwh_modbus import FranklinWHController


def pytest_collection_modifyitems(items):
    for item in items:
        if "hardware" in str(item.fspath):
            item.add_marker(pytest.mark.hardware)


@pytest.fixture(scope="session")
def agate_host():
    return os.environ.get("FRANKLINWH_TEST_HOST", "192.168.1.100")


@pytest.fixture(scope="session")
def agate_port():
    return int(os.environ.get("FRANKLINWH_TEST_PORT", "502"))


@pytest.fixture(scope="session")
def agate_unit():
    return int(os.environ.get("FRANKLINWH_TEST_UNIT", "1"))


@pytest.fixture(scope="session")
def controller(agate_host, agate_port, agate_unit):
    """Connect to the real aGate. Session-scoped to avoid reconnecting per test."""
    ctrl = FranklinWHController(agate_host, port=agate_port, unit_id=agate_unit)
    connected = ctrl.connect()
    if not connected:
        pytest.skip(f"Cannot connect to aGate at {agate_host}:{agate_port}")
    yield ctrl
    ctrl.disconnect()
