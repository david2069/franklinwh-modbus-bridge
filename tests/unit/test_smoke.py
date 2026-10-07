"""Smoke tests — verify the package imports and basic structure."""


def test_package_imports():
    import franklinwh_bridge

    assert franklinwh_bridge.__version__ == "0.2.2"


def test_app_creates():
    from franklinwh_bridge.main import app

    assert app.title == "franklinwh-modbus-bridge"


def test_cli_creates():
    from franklinwh_bridge.cli import app

    assert app.info.name == "bridge"
