"""VPN fleet control route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_fleet_routes import register_vpn_fleet_routes


class FakeManager:
    def __init__(self):
        self.current_ip = "1.2.3.4"
        self.current_server = {"hostname": "se1"}
        self.refreshed = 0

    async def refresh_status(self):
        self.refreshed += 1


class FakeFleet:
    def __init__(self):
        self.calls = []

    async def set_enabled_all(self, enabled):
        self.calls.append(("toggle", enabled))
        return {"ok": True, "enabled": enabled}

    async def connect_all(self):
        self.calls.append(("connect",))
        return {"ok": True, "connected": 6}

    async def disconnect_all(self, stop_containers=False):
        self.calls.append(("disconnect", stop_containers))
        return {"ok": True}


def _app(*, managers=None, single=None, token_error=None):
    fleet = FakeFleet()
    app = FastAPI()
    register_vpn_fleet_routes(
        app,
        token_checker=lambda request: token_error,
        managers_getter=lambda: list(managers or []),
        single_manager_getter=lambda: single,
        fleet_getter=lambda: fleet,
    )
    return app, fleet


def test_toggle_and_disconnect():
    manager = FakeManager()
    app, fleet = _app(managers=[manager])
    client = TestClient(app)
    assert client.post("/api/vpn/toggle", json={"enabled": True}).json()["enabled"] is True
    assert client.post("/api/vpn/disconnect").json()["ok"] is True
    assert fleet.calls == [("toggle", True), ("disconnect", False)]


def test_connect_returns_first_station_ip_and_refreshes():
    manager = FakeManager()
    app, fleet = _app(managers=[manager])
    payload = TestClient(app).post("/api/vpn/connect").json()
    assert payload["connected"] == 6
    assert payload["ip"] == "1.2.3.4"
    assert payload["server"] == {"hostname": "se1"}
    assert manager.refreshed == 1


def test_single_manager_fallback_and_missing():
    app, fleet = _app(managers=[], single=FakeManager())
    assert TestClient(app).post("/api/vpn/toggle", json={"enabled": False}).status_code == 200
    app2, _ = _app(managers=[], single=None)
    assert "non initialisé" in TestClient(app2).post("/api/vpn/connect").json()["error"]


def test_token_rejection():
    app, fleet = _app(
        managers=[FakeManager()],
        token_error=JSONResponse({"error": "token"}, status_code=401),
    )
    assert TestClient(app).post("/api/vpn/toggle", json={}).status_code == 401
    assert fleet.calls == []
