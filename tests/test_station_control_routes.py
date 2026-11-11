"""Station control route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.station_control_routes import (
    register_station_control_routes,
)


class FakeManager:
    def __init__(self):
        self._station = 1
        self._error = "old"
        self._auth_failed = True
        self._stack_effective = "wireguard"
        self.health = []
        self.status = None

    async def pin_country(self, country):
        return country == "Japan"

    async def unpin_country(self):
        return True

    async def get_public_ip(self):
        return "1.2.3.4"

    async def restart(self):
        raise TimeoutError()

    async def set_stack(self, stack, **kwargs):
        return {"ok": True}

    def _note_pool_health(self, ok, cause=""):
        self.health.append((ok, cause))

    def _set_status(self, status):
        self.status = status


class FakePool:
    def __init__(self):
        self.rotated = []

    def _launch_rotation(self, manager):
        self.rotated.append(manager._station)


class FakeFleet:
    def __init__(self):
        self.calls = []

    async def start_station(self, sid):
        self.calls.append(("start", sid))
        return {"ok": True}

    async def stop_station(self, sid):
        self.calls.append(("stop", sid))
        return {"ok": True}

    async def connect_station(self, sid):
        self.calls.append(("connect", sid))
        return {"ok": True}

    async def disconnect_station(self, sid):
        self.calls.append(("disconnect", sid))
        return {"ok": True}

    async def connect_all(self):
        return {"ok": True, "action": "connect_all"}

    async def disconnect_all(self, stop_containers=False):
        return {"ok": True, "stop": stop_containers}

    def status(self):
        return {"stations": 6}


def _app(*, managers=None, pool=None, fleet=None):
    app = FastAPI()
    register_station_control_routes(
        app,
        token_checker=lambda request: None,
        managers_getter=lambda: list(managers or []),
        pool_getter=lambda: pool,
        fleet_getter=lambda: fleet or FakeFleet(),
    )
    return app


def test_pin_country_and_validation():
    client = TestClient(_app(managers=[FakeManager()]))
    assert client.post(
        "/api/vpn/station/1/pin_country",
        json={"country": "Japan"},
    ).json() == {"ok": True, "station": 1, "country": "Japan"}
    assert client.post(
        "/api/vpn/station/1/pin_country",
        json={"country": "Nowhere"},
    ).status_code == 502
    assert client.post(
        "/api/vpn/station/1/pin_country",
        json={},
    ).status_code == 400
    assert client.post(
        "/api/vpn/station/2/pin_country",
        json={"country": "Japan"},
    ).status_code == 404


def test_probe_marks_health_and_status():
    manager = FakeManager()
    payload = TestClient(_app(managers=[manager])).post(
        "/api/vpn/station/1/probe"
    ).json()
    assert payload == {"ok": True, "station": 1, "ip": "1.2.3.4"}
    assert manager.health == [(True, "probe"), (True, "probe")]
    assert manager.status == "connected"
    assert manager._auth_failed is False


def test_restart_timeout_is_reported():
    payload = TestClient(_app(managers=[FakeManager()])).post(
        "/api/vpn/station/1/restart"
    ).json()
    assert payload["ok"] is True
    assert "SSE" in payload["note"]


def test_stack_and_soft_rotate():
    manager = FakeManager()
    pool = FakePool()
    client = TestClient(_app(managers=[manager], pool=pool))
    assert client.post(
        "/api/vpn/station/1/stack",
        json={"stack": "openvpn"},
    ).json()["ok"] is True
    assert client.post(
        "/api/vpn/station/1/stack",
        json={"stack": "bad"},
    ).status_code == 400
    assert client.post(
        "/api/vpn/station/1/soft-rotate"
    ).json()["queued"] is True
    assert pool.rotated == [1]


def test_fleet_actions():
    fleet = FakeFleet()
    client = TestClient(_app(fleet=fleet))
    assert client.post("/api/vpn/fleet/connect").json()["action"] == "connect_all"
    assert client.post("/api/vpn/fleet/disconnect").json()["stop"] is False
    assert client.get("/api/vpn/fleet").json()["stations"] == 6
    assert client.post("/api/vpn/station/1/start").json()["ok"] is True
    assert client.post("/api/vpn/station/1/stop").json()["ok"] is True
    assert fleet.calls == [("start", 1), ("stop", 1)]
