"""GET /api/points must be scoped to the DEFAULT gateway's bus.

Regression: it read the global sample bus, so when another gateway (a mock,
or a second gateway on the same aGate) published a partial sample, the
default dashboard flickered between gateways' data and blanked real fields.
"""

from types import SimpleNamespace

from franklinwh_bridge.api.admin import get_points
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
