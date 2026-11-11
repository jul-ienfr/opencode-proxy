"""Station inventory and reconcile-plan route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.station_routes import register_station_routes


class FakeHealth:
    def snapshot(self):
        return {1: {"state": "client_usable", "usable": True}}


class FakeEndpoint:
    _station = 0
    container = "warp-1"
    label = "warp-fr"
    egress_ip = "5.6.7.8"
    ipv6 = False
    managed = True
    enabled = True
    host = "127.0.0.1"
    port = 1080
    block_key = "5.6.7.8/32"
    block = "5.6.7.8/32"
    reset_at = None


class FakePool:
    _station_health = FakeHealth()

    def _socks5_enabled_eps(self):
        return [FakeEndpoint()]

    def _socks5_client_usable(self, endpoint):
        return True

    def duplicate_egress_groups(self):
        return {"1.2.3.4/32": [1]}


class FakeManager:
    _station = 1
    _docker_container = "nord-1"
    _disabled = False

    def get_status(self):
        return {
            "container": "nord-1",
            "status": "connected",
            "ip": "1.2.3.4",
            "assigned_stack": "wireguard",
            "ovpn_protocol_effective": "udp",
            "current_host": "se1.nordvpn.com",
            "assigned_country": "Sweden",
            "proxy_port": 1080,
        }


def _app(*, pool=None, managers=None, token_error=None):
    app = FastAPI()
    register_station_routes(
        app,
        token_checker=lambda request: token_error,
        pool_getter=lambda: pool,
        managers_getter=lambda: list(managers or [FakeManager()]),
        config_getter=lambda: {
            "egress_capacity": {"target_slots": 6, "reserve_policy": "full"}
        },
        capacity_counts_getter=lambda: {"nordvpn": {"desired": 6}},
    )
    return app


def test_routes_are_registered():
    app = _app()
    paths = {route.path for route in app.routes}
    assert "/api/stations" in paths
    assert "/api/stations/reconcile-plan" in paths
    assert "/api/stations/{provider}/{station}" in paths


def test_token_rejection_is_returned_as_is():
    client = TestClient(_app(token_error=JSONResponse({"error": "token"}, status_code=401)))
    assert client.get("/api/stations").status_code == 401


def test_stations_payload_includes_nordvpn_and_warp():
    client = TestClient(_app(pool=FakePool()))
    payload = client.get("/api/stations").json()
    assert payload["target_slots"] == 6
    assert payload["reserve_policy"] == "full"
    assert payload["providers"] == {"nordvpn": {"desired": 6}}
    stations = {row["provider"]: row for row in payload["stations"]}
    assert stations["nordvpn"]["station"] == 1
    assert stations["nordvpn"]["client_usable"] is True
    assert stations["nordvpn"]["egress_ip"] == "1.2.3.4"
    assert stations["nordvpn"]["block"] == "1.2.3.4/32"
    assert stations["nordvpn"]["provider_details"]["hostname"] == "se1.nordvpn.com"
    assert stations["warp"]["client_usable"] is True
    assert stations["warp"]["egress_ip"] == "5.6.7.8"
    assert stations["warp"]["block"] == "5.6.7.8/32"
    assert payload["duplicate_buckets"] == {"1.2.3.4/32": [1]}
    assert payload["warnings"] == ["duplicate_block:1.2.3.4/32"]


def test_stations_payload_without_pool_is_fail_soft():
    client = TestClient(_app(pool=None))
    payload = client.get("/api/stations").json()
    assert [row["provider"] for row in payload["stations"]] == ["nordvpn"]
    assert payload["duplicate_buckets"] == {}
    assert payload["warnings"] == []


def test_station_detail_and_unknown_station():
    client = TestClient(_app(pool=FakePool()))
    detail = client.get("/api/stations/nordvpn/1").json()
    assert detail["provider"] == "nordvpn"
    assert detail["station"] == 1
    missing = client.get("/api/stations/nordvpn/9").json()
    assert missing == {"error": "station inconnue", "provider": "nordvpn", "station": 9}


def test_reconcile_plan_is_read_only():
    client = TestClient(_app(pool=FakePool()))
    payload = client.get("/api/stations/reconcile-plan").json()
    assert payload["read_only"] is True
    assert isinstance(payload["decisions"], list)
    assert payload["generated_at"]
