"""VPN health-check route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_health_routes import register_vpn_health_routes


class FakeManager:
    async def health_check(self):
        return {"ok": True, "ip": "1.2.3.4"}


def _app(*, manager=None, token_error=None):
    app = FastAPI()
    register_vpn_health_routes(
        app,
        token_checker=lambda request: token_error,
        manager_getter=lambda: manager,
    )
    return app


def test_health_check_returns_manager_result():
    payload = TestClient(_app(manager=FakeManager())).post(
        "/api/vpn/health-check"
    ).json()
    assert payload == {"ok": True, "ip": "1.2.3.4"}


def test_missing_manager_is_explicit():
    payload = TestClient(_app(manager=None)).post("/api/vpn/health-check").json()
    assert "non initialisé" in payload["error"]


def test_token_rejection():
    app = _app(
        manager=FakeManager(),
        token_error=JSONResponse({"error": "token"}, status_code=401),
    )
    assert TestClient(app).post("/api/vpn/health-check").status_code == 401
