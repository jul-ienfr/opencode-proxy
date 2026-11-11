"""VPN control route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_control_routes import register_vpn_control_routes


def _app(*, token_error=None):
    engine = SimpleNamespace(paused=False)
    logs: list[str] = []

    def validate(body):
        return [] if body.get("enabled") else ["champ requis"]

    app = FastAPI()
    register_vpn_control_routes(
        app,
        token_checker=lambda request: token_error,
        validate_payload=validate,
        engine_getter=lambda: engine,
        debug_logger=logs.append,
    )
    return app, engine, logs


def test_validate_ok_and_errors():
    client = TestClient(_app()[0])
    ok = client.post("/api/vpn-config/validate", json={"enabled": True}).json()
    assert ok == {"valid": True, "errors": []}
    bad = client.post("/api/vpn-config/validate", json={}).json()
    assert bad == {"valid": False, "errors": ["champ requis"]}


def test_validate_invalid_json_and_token():
    app, _, _ = _app(token_error=JSONResponse({"error": "token"}, status_code=401))
    assert TestClient(app).post("/api/vpn-config/validate", json={}).status_code == 401
    client = TestClient(_app()[0])
    invalid = client.post(
        "/api/vpn-config/validate",
        content="{bad",
        headers={"content-type": "application/json"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["valid"] is False


def test_rotation_paused_sets_engine_and_logs():
    app, engine, logs = _app()
    payload = TestClient(app).post(
        "/api/vpn/rotation-paused",
        json={"paused": True},
    ).json()
    assert payload == {"ok": True, "paused": True}
    assert engine.paused is True
    assert "rotation_paused" in logs[0]
