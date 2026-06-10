"""GET /api/points must be scoped to the DEFAULT gateway's bus.

Regression: it read the global sample bus, so when another gateway (a mock,
or a second gateway on the same aGate) published a partial sample, the
default dashboard flickered between gateways' data and blanked real fields.
"""

from types import SimpleNamespace

from franklinwh_bridge.api.admin import get_points, read_model
from franklinwh_bridge.modbus.sample import Sample, SampleBus


async def test_points_use_default_gateway_bus_not_global():
    # Default gateway's own bus: a full, real-looking sample.
    default_bus = SampleBus()
    await default_bus.publish(
        Sample.now("default", {"soc": 50, "model": "aGate X", "voltage": 240})
    )
    # Global bus: a DIFFERENT gateway's partial sample, published more recently.
    global_bus = SampleBus()
    await global_bus.publish(Sample.now("mock1", {"soc": 99}))

    default_inst = SimpleNamespace(sample_bus=default_bus, command_handler=None)
    registry = SimpleNamespace(
        get=lambda gid: default_inst if gid == "default" else None
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(registry=registry, sample_bus=global_bus)
        )
    )

    resp = await get_points(request)
    assert resp["gateway_id"] == "default"
    assert resp["points"]["soc"] == 50  # default's value, not the mock's 99
    assert resp["points"]["model"] == "aGate X"  # full field, not blanked


async def test_read_model_resolves_controller_from_registry():
    """Regression: /api/models/{id}/read read app.state.controller (None under
    multi-gateway) and 503'd. It must resolve the default gateway's controller."""
    class _Pt:
        def __init__(self, v):
            self.value = v

    class _Model:
        points = {"OutWh": _Pt(13280454), "ID": _Pt(502)}
        def read(self):
            pass

    class _Ctrl:
        dev = object()  # already "connected"
        def connect(self):
            pass
        def disconnect(self):
            pass
        def get_model(self, mid):
            return _Model() if mid == 502 else None

    inst = SimpleNamespace(
        controller=_Ctrl(), modbus_lock=None, sample_bus=SampleBus()
    )
    registry = SimpleNamespace(get=lambda gid: inst if gid == "default" else None)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                registry=registry, controller=None, sample_bus=SampleBus()
            )
        )
    )

    resp = await read_model(502, request)
    assert resp["model_id"] == 502
    assert resp["values"]["502.OutWh"] == 13280454
