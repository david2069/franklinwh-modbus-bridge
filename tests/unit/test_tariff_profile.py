"""Tariff profiles — export, share, import.

A profile describes a TARIFF, not a customer. Sharing a plan must not share the
meter number, the account, or the network's approval of one connection.
"""

from __future__ import annotations

from franklinwh_bridge.api.gateways_api import _PORTABLE_SERVICE_FIELDS

IDENTIFYING = ("meter_number", "account", "pto_status", "pto_date", "pto_reference",
               "export_limit_kw", "export_allowed", "charging_allowed",
               "discharging_allowed", "enabled", "plan_version", "plan_started_at")


def test_identifying_fields_never_travel():
    """The point of the allow-list. A connection's approval and account belong
    to one site; a tariff profile is meant to be handed to a stranger."""
    for field in IDENTIFYING:
        assert field not in _PORTABLE_SERVICE_FIELDS, f"{field} must not be exported"


def test_the_tariff_itself_does_travel():
    for field in ("pricing", "retailer", "network", "plan_type",
                  "demand_window", "bonus_window"):
        assert field in _PORTABLE_SERVICE_FIELDS


def test_network_travels_but_its_approval_does_not():
    """Naming the DNSP is part of describing a plan (its two-way tariff applies
    to everyone on that network). The PTO granted to one connection is not."""
    assert "network" in _PORTABLE_SERVICE_FIELDS
    assert "pto_status" not in _PORTABLE_SERVICE_FIELDS
    assert "pto_reference" not in _PORTABLE_SERVICE_FIELDS
