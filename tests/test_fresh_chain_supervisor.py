"""Supervisor recovery predicate tests (no Docker, no network)."""

from __future__ import annotations

from scripts import fresh_chain_supervisor as supervisor


def _station(**overrides):
    value = {
        "station_usable": True,
        "model_usable": True,
        "model_usable_reason": None,
        "health": {"state": "healthy"},
    }
    value.update(overrides)
    return value


def test_recovered_requires_a_fresh_model_proof():
    assert supervisor._station_recovered(_station()) is True
    assert (
        supervisor._station_recovered(
            _station(model_usable=False, model_usable_reason="probe-error")
        )
        is False
    )
    assert (
        supervisor._station_recovered(
            _station(model_usable=False, model_usable_reason="quota-429")
        )
        is False
    )


def test_recovered_rejects_an_unusable_tunnel():
    assert (
        supervisor._station_recovered(_station(station_usable=False)) is False
    )


def test_recovered_rejects_a_transitional_health_state():
    for state in ("repairing", "probing", "suspect", "parked"):
        assert (
            supervisor._station_recovered(_station(health={"state": state}))
            is False
        )
