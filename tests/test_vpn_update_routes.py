"""VPN update route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_update_routes import register_vpn_update_routes


class FakeManager:
    def __init__(self, *, available=True, opportune=True):
        self.available = available
        self.opportune = opportune
        self.checked = 0

    async def check_update(self):
        self.checked += 1
        return self.available

    def _update_opportune(self):
        return self.opportune

    def get_status(self):
        return {"update": {"available": self.available}}


def _app(*, manager=None, token_error=None, apply_error=None):
    applied = []

    async def apply_update(managers, primary):
        if apply_error:
            raise RuntimeError(apply_error)
        applied.append((len(managers), primary))
        return {"ok": True, "updated": 6}

    app = FastAPI()
    register_vpn_update_routes(
        app,
        token_checker=lambda request: token_error,
        manager_getter=lambda: manager,
        managers_getter=lambda: [manager] if manager else [],
        apply_update=apply_update,
    )
    return app, applied


def test_update_success_applies_fleet_update():
    manager = FakeManager()
    app, applied = _app(manager=manager)
    payload = TestClient(app).post("/api/vpn/update").json()
    assert payload["ok"] is True
    assert payload["updated"] == 6
    assert payload["update"] == {"available": True}
    assert applied == [(1, manager)]


def test_update_unavailable_or_not_opportune():
    app, applied = _app(manager=FakeManager(available=False))
    payload = TestClient(app).post("/api/vpn/update").json()
    assert payload["ok"] is False
    assert "aucune mise" in payload["error"]
    app2, applied2 = _app(manager=FakeManager(opportune=False))
    payload = TestClient(app2).post("/api/vpn/update").json()
    assert "pas opportun" in payload["error"]
    assert applied == [] and applied2 == []


def test_update_missing_manager_error_and_apply_exception():
    app, _ = _app(manager=None)
    assert "non initialisé" in TestClient(app).post("/api/vpn/update").json()["error"]
    app2, _ = _app(manager=FakeManager(), apply_error="boom")
    assert "boom" in TestClient(app2).post("/api/vpn/update").json()["error"]


def test_update_token_rejection():
    app, applied = _app(
        manager=FakeManager(),
        token_error=JSONResponse({"error": "token"}, status_code=401),
    )
    assert TestClient(app).post("/api/vpn/update").status_code == 401
    assert applied == []
