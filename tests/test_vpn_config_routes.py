"""Read-only VPN config and stack-info route tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.vpn_config_routes import (
    build_vpn_config,
    build_vpn_stack_info,
    register_vpn_config_read_routes,
)


class FakeManager:
    _station = 2

    def get_config(self):
        return {"enabled": True, "servers": ["a"], "proxy_mode": "vpn"}

    def stack_info(self):
        return {"stack": "wireguard"}


def test_disabled_config_when_no_manager():
    payload = build_vpn_config(
        None,
        rotation_config={},
        quality_blocks=("quality",),
        free_policy_getter=lambda: "auto",
    )
    assert payload["enabled"] is False
    assert payload["free_substitution_policy"] == "auto"


def test_config_merges_quality_and_egress():
    payload = build_vpn_config(
        FakeManager(),
        rotation_config={
            "quality": {"min": 1},
            "egress_providers": ["warp"],
            "egress_strategy": "strict",
            "egress_weights": {"warp": 0.5},
        },
        quality_blocks=("quality",),
        free_policy_getter=lambda: "auto",
    )
    assert payload["quality"] == {"min": 1}
    assert payload["egress_providers"] == ["warp"]
    assert payload["egress_strategy"] == "strict"
    assert payload["egress_weights"] == {"warp": 0.5}


def test_stack_info_per_station():
    assert build_vpn_stack_info([FakeManager()]) == {
        "stations": {"2": {"stack": "wireguard"}}
    }


def test_routes_are_registered_and_serve():
    app = FastAPI()
    register_vpn_config_read_routes(
        app,
        manager_getter=lambda: FakeManager(),
        managers_getter=lambda: [FakeManager()],
        rotation_config_getter=lambda: {},
        free_policy_getter=lambda: "auto",
        quality_blocks=(),
    )
    client = TestClient(app)
    assert client.get("/api/vpn-config").json()["enabled"] is True
    assert client.get("/api/vpn-stack-info").json()["stations"]["2"]["stack"] == (
        "wireguard"
    )
