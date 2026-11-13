"""Supervisor recovery predicate tests (no Docker, no network)."""

from __future__ import annotations

import subprocess

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


def test_needs_chain_only_for_unusable_stations():
    healthy = {
        "current_ip": "203.0.113.7",
        "station_usable": True,
        "model_usable": True,
        "health": {"state": "healthy"},
    }
    assert supervisor._needs_chain(healthy) is False
    assert (
        supervisor._needs_chain({**healthy, "model_usable": False}) is True
    )
    assert supervisor._needs_chain({**healthy, "current_ip": ""}) is True
    assert (
        supervisor._needs_chain({**healthy, "station_usable": False}) is True
    )


def test_any_model_proof_counts_as_usable():
    station = _station(
        model_usable=False,
        model_usable_by_model={"m2": {"usable": True}},
    )
    assert supervisor._station_recovered(station) is True
    assert (
        supervisor._needs_chain(
            {
                "current_ip": "203.0.113.7",
                "station_usable": True,
                "model_usable": False,
                "model_usable_by_model": {"m2": {"usable": True}},
                "health": {"state": "healthy"},
            }
        )
        is False
    )


def test_credential_refresh_respects_the_cooldown(tmp_path, monkeypatch):
    script = tmp_path / "scripts" / "refresh_nord_credentials.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(supervisor, "ROOT", tmp_path)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0] if args else [], 0, "", "")

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)

    assert supervisor._refresh_credentials_if_due(now=1_800_000_000.0) is True
    assert supervisor._refresh_credentials_if_due(now=1_800_000_001.0) is False
    assert len(calls) == 1
