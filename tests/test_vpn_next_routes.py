"""Next-server VPN route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_next_routes import register_vpn_next_routes


class FakeManager:
    def __init__(self, station=1):
        self._station = station
        self.current_ip = "1.1.1.1"
        self.current_server = {"hostname": f"se{station}"}

    async def connect_next(self):
        self.current_ip = "2.2.2.2"
        self.current_server = {"hostname": "jp1"}


class FakePool:
    def __init__(self, manager):
        self.active_station = manager
        self.calls = []

    async def switch_ip(self, station):
        self.calls.append(station._station)
        station.current_ip = "9.9.9.9"
        station.current_server = {"hostname": "us1"}


def _app(*, managers=None, pool=None, single=None, token_error=None):
    app = FastAPI()
    register_vpn_next_routes(
        app,
        token_checker=lambda request: token_error,
        managers_getter=lambda: list(managers or []),
        single_manager_getter=lambda: single,
        pool_getter=lambda: pool,
    )
    return app


def test_next_with_pool_uses_selected_station():
    first, second = FakeManager(1), FakeManager(2)
    pool = FakePool(first)
    payload = TestClient(_app(managers=[first, second], pool=pool)).post(
        "/api/vpn/next",
        json={"station": 2},
    ).json()
    assert payload == {
        "ok": True,
        "ip": "9.9.9.9",
        "server": {"hostname": "us1"},
        "station": 2,
    }
    assert pool.calls == [2]


def test_next_without_pool_connects_next():
    manager = FakeManager()
    payload = TestClient(_app(managers=[manager], pool=None)).post(
        "/api/vpn/next",
        json={},
    ).json()
    assert payload["ok"] is True
    assert payload["ip"] == "2.2.2.2"
    assert payload["station"] == 1


def test_next_validation_errors():
    manager = FakeManager()
    client = TestClient(_app(managers=[manager], pool=None))
    assert "non configurée" in client.post(
        "/api/vpn/next",
        json={"station": 9},
    ).json()["error"]
    empty = TestClient(_app(managers=[], pool=None))
    assert "non initialisé" in empty.post("/api/vpn/next", json={}).json()["error"]


def test_next_token_rejection():
    app = _app(
        managers=[FakeManager()],
        token_error=JSONResponse({"error": "token"}, status_code=401),
    )
    assert TestClient(app).post("/api/vpn/next", json={}).status_code == 401
