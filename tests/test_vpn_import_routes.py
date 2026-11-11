"""VPN import route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_import_routes import register_vpn_import_routes


class FakeManager:
    pass


def _app(*, managers=None, single=None, status=200, token_error=None):
    calls = []

    async def apply_import(items, body, **kwargs):
        calls.append((len(items), body, kwargs))
        return {"ok": status == 200}, status

    app = FastAPI()
    register_vpn_import_routes(
        app,
        token_checker=lambda request: token_error,
        managers_getter=lambda: list(managers or []),
        single_manager_getter=lambda: single,
        apply_import=apply_import,
    )
    return app, calls


def test_import_success_and_payload():
    manager = FakeManager()
    app, calls = _app(managers=[manager], single=manager)
    payload = TestClient(app).post(
        "/api/vpn/import",
        json={"server": "se1"},
    ).json()
    assert payload == {"ok": True}
    assert calls[0][0] == 1
    assert calls[0][1] == {"server": "se1"}
    assert calls[0][2]["fallback_manager"] is manager


def test_import_error_status_and_token():
    app, _ = _app(status=400)
    assert TestClient(app).post("/api/vpn/import", json={}).status_code == 400
    token_app, calls = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(token_app).post("/api/vpn/import", json={}).status_code == 401
    assert calls == []
