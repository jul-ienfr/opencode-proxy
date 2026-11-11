"""Retry-config route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.retry_routes import register_retry_routes


def _app(*, token_error=None, persist_error=None):
    config = {"enabled": False, "max_wait_s": 30}
    persisted: list[dict] = []

    def persist(updates):
        persisted.append(dict(updates))
        if persist_error:
            return persist_error
        config.update(updates)
        return None

    app = FastAPI()
    register_retry_routes(
        app,
        config_getter=lambda: dict(config),
        config_persister=persist,
        token_checker=lambda request: token_error,
    )
    return app, persisted


def test_routes_are_registered():
    app, _ = _app()
    paths = {route.path for route in app.routes}
    assert "/api/retry-config" in paths


def test_get_returns_effective_config():
    app, _ = _app()
    payload = TestClient(app).get("/api/retry-config").json()
    assert payload == {"enabled": False, "max_wait_s": 30}


def test_token_rejection():
    app, persisted = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    response = TestClient(app).post("/api/retry-config", json={"enabled": True})
    assert response.status_code == 401
    assert persisted == []


def test_update_normalizes_string_boolean():
    app, persisted = _app()
    payload = TestClient(app).post(
        "/api/retry-config",
        json={"enabled": "yes", "max_wait_s": 45},
    ).json()
    assert persisted == [{"enabled": True, "max_wait_s": 45}]
    assert payload["enabled"] is True
    assert payload["max_wait_s"] == 45


def test_invalid_json_and_non_object_are_400():
    client = TestClient(_app()[0])
    invalid = client.post(
        "/api/retry-config",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert invalid.status_code == 400
    bad_type = client.post("/api/retry-config", json=["enabled"])
    assert bad_type.status_code == 400


def test_empty_update_returns_config_and_persist_error_is_400():
    app, persisted = _app()
    client = TestClient(app)
    assert client.post("/api/retry-config", json={}).status_code == 200
    assert persisted == []
    app2, _ = _app(persist_error="lecture seule")
    response = TestClient(app2).post("/api/retry-config", json={"enabled": True})
    assert response.status_code == 400
    assert response.json()["error"] == "lecture seule"
