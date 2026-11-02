"""Offline contracts for the all-station repair supervisor."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "fresh_chain_supervisor.py"
    spec = importlib.util.spec_from_file_location("fresh_chain_supervisor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_healthy_station_never_gets_a_chain():
    module = _module()
    assert module._needs_chain(
        {"station_usable": True, "model_usable": True, "current_ip": "1.2.3.4"}
    ) is False


def test_quota_or_error_station_needs_a_chain():
    module = _module()
    assert module._needs_chain(
        {
            "station_usable": True,
            "model_usable": False,
            "model_usable_reason": "daily-bucket-exhausted",
        }
    )
    assert module._needs_chain(
        {"station_usable": False, "vpn_status": "error", "current_ip": "1.2.3.4"}
    )
    assert module._needs_chain(
        {"station_usable": True, "model_usable": True, "current_ip": ""}
    )


def test_transient_model_cooldown_does_not_rotate(monkeypatch):
    module = _module()
    assert (
        module._needs_chain(
            {
                "station_usable": True,
                "model_usable": False,
                "model_usable_reason": "model-cooldown",
                "current_ip": "1.2.3.4",
            }
        )
        is False
    )


def test_exhaustion_pause_requires_sustained_zero_usable_polls():
    module = _module()
    assert module._exhaustion_due(2, 100, 40) is False
    assert module._exhaustion_due(3, 39, 40) is False
    assert module._exhaustion_due(3, 40, 40) is True
    assert module._exhaustion_due(10, 10**6, 40) is True


def test_scope_honours_station_count_and_operator_disabled(monkeypatch):
    module = _module()

    def _fake_yaml_get(section, key, default=None):
        if (section, key) == ("ip_rotation", "station_count"):
            return 4
        if (section, key) == ("ip_rotation", "disabled_stations"):
            return [2]
        return default

    monkeypatch.setattr(module, "yaml_get", _fake_yaml_get)
    assert module._scope() == [1, 3, 4]


def test_need_rank_prefers_tunnel_up_quota_station():
    module = _module()
    quota = {"station_usable": True, "model_usable": False, "vpn_status": "connected"}
    connected = {"station_usable": False, "model_usable": False, "vpn_status": "connected"}
    down = {"station_usable": False, "model_usable": False, "vpn_status": "error"}
    assert module._need_rank(quota) < module._need_rank(connected) < module._need_rank(down)


def test_manager_owned_repair_blocks_allocator():
    module = _module()
    station = {
        "current_ip": "1.2.3.4",
        "station_usable": False,
        "model_usable": False,
        "vpn_status": "error",
        "health": {"state": "repairing", "repairing_s": 30},
    }
    assert module._needs_chain(station) is False
    station["health"] = {"state": "repairing", "repairing_s": 601}
    assert module._needs_chain(station) is True
    station["health"] = {"state": "parked", "parked_remaining_s": 100}
    assert module._needs_chain(station) is False


def test_egress_park_is_repairable_but_auth_park_is_not():
    module = _module()
    base = {
        "current_ip": "1.2.3.4",
        "station_usable": False,
        "model_usable": False,
        "vpn_status": "error",
    }
    egress_park = dict(
        base,
        health={
            "state": "parked",
            "cause": "deadline 240s",
            "last_error": "egress",
            "parked_remaining_s": 1500,
        },
    )
    auth_park = dict(
        base,
        health={
            "state": "parked",
            "cause": "auth_failed",
            "last_error": "AUTH_FAILED",
            "parked_remaining_s": 1500,
        },
    )
    assert module._needs_chain(egress_park) is True
    assert module._needs_chain(auth_park) is False
