"""Custom routes/web tools/API-key config route tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers import config_extra_routes


def _app(*, token_error=None, custom_error=None):
    yaml = {}
    saved_routes = []
    saved_keys = []
    env = []
    debug = []
    merged = []
    settings = SimpleNamespace(CUSTOM_ROUTES=[{"path": "/x"}])

    def yaml_get(section, key, default=None):
        return yaml.get((section, key), default)

    def yaml_set(section, key, value):
        yaml[(section, key)] = value

    def validate(body):
        return custom_error

    def save_routes(body):
        saved_routes.append(body)

    def merge(rows, replace=False):
        merged.append((rows, replace))
        return [dict(row, merged=True) for row in rows]

    def save_keys(rows):
        saved_keys.append(rows)

    app = FastAPI()
    config_extra_routes.register_config_extra_routes(
        app,
        token_checker=lambda request: token_error,
        settings=settings,
        models_getter=lambda: {"model-a": {}, "model-b": {}},
        native_models=["model-a"],
        yaml_get=yaml_get,
        yaml_set=yaml_set,
        validate_custom_routes=validate,
        save_custom_routes=save_routes,
        api_keys_getter=lambda: [
            {
                "api_key": "abcdefghijkl",
                "go_workspace_id": "workspace-1",
                "go_auth_cookie": "cookie-1",
                "alias": "main",
            }
        ],
        api_key_routing_getter=lambda: "round-robin",
        merge_preserved_api_keys=merge,
        save_api_keys=save_keys,
        save_env=lambda updates: env.append(updates),
        debug=debug.append,
    )
    return app, yaml, saved_routes, saved_keys, env, debug, merged


def test_custom_routes_get_post_and_validation():
    app, _, saved, *_ = _app()
    client = TestClient(app)
    assert client.get("/api/config/custom-routes").json() == [{"path": "/x"}]
    response = client.post(
        "/api/config/custom-routes",
        json={"path": "/y"},
    ).json()
    assert response == {"status": "ok", "message": "Custom routes updated."}
    assert saved == [{"path": "/y"}]

    bad_app, _, _, _, _, _, _ = _app(custom_error="bad shape")
    bad = TestClient(bad_app).post(
        "/api/config/custom-routes",
        json={},
    )
    assert bad.status_code == 400
    assert "custom_routes invalide" in bad.json()["error"]


def test_web_search_get_and_update():
    app, yaml, _, _, _, _, _ = _app()
    client = TestClient(app)
    initial = client.get("/api/config/web-search").json()
    assert initial["mode"] == "duckduckgo"
    assert initial["web_fetch"]["mode"] == "direct"
    assert initial["available_models"] == ["model-a", "model-b"]
    client.post(
        "/api/config/web-search",
        json={
            "mode": "model",
            "target_model": "model-a",
            "max_results": 99,
            "timeout": 1,
            "enabled": "yes",
            "via_vpn": "on",
            "on_error": "strip",
            "web_fetch": {
                "mode": "model",
                "target_model": "model-b",
                "max_bytes": 99999,
                "timeout": 1,
                "enabled": "true",
                "via_vpn": "1",
                "on_error": "strip",
            },
        },
    )
    assert yaml[("web_search", "mode")] == "model"
    assert yaml[("web_search", "max_results")] == 10
    assert yaml[("web_search", "timeout")] == 5
    assert yaml[("web_search", "enabled")] is True
    assert yaml[("web_search", "via_vpn")] is True
    assert yaml[("web_fetch", "mode")] == "model"
    assert yaml[("web_fetch", "max_bytes")] == 50000
    assert yaml[("web_fetch", "timeout")] == 5
    assert yaml[("web_fetch", "enabled")] is True


def test_web_fetch_get_and_update():
    app, yaml, *_ = _app()
    client = TestClient(app)
    assert client.get("/api/config/web-fetch").json()["modes"] == [
        "direct",
        "model",
        "direct_then_model",
    ]
    client.post(
        "/api/config/web-fetch",
        json={
            "mode": "direct_then_model",
            "target_model": "model-a",
            "max_bytes": 100,
            "timeout": 99,
            "enabled": False,
            "via_vpn": True,
            "on_error": "strip",
        },
    )
    assert yaml[("web_fetch", "mode")] == "direct_then_model"
    assert yaml[("web_fetch", "max_bytes")] == 2000
    assert yaml[("web_fetch", "timeout")] == 30
    assert yaml[("web_fetch", "enabled")] is False
    assert yaml[("web_fetch", "via_vpn")] is True


def test_api_keys_mask_and_update(monkeypatch):
    rebuilt = []

    class FakeOpencode:
        API_KEY_ROUTING = ""

        @staticmethod
        def _rebuild_key_cache():
            rebuilt.append(True)

    monkeypatch.setitem(sys.modules, "opencode", FakeOpencode)
    app, _, _, saved, env, debug, merged = _app()
    client = TestClient(app)
    payload = client.get("/api/config/api-keys").json()
    assert payload["api_keys"][0]["api_key_masked"] == "abcd****ijkl"
    assert payload["api_keys"][0]["has_go_workspace"] is True
    assert payload["count"] == 1
    assert payload["routing"] == "round-robin"
    response = client.post(
        "/api/config/api-keys",
        json={
            "api_keys": [{"api_key": "new"}],
            "replace": True,
            "routing": "failover",
        },
    ).json()
    assert response == {"status": "ok", "message": "API keys saved."}
    assert merged == [([{"api_key": "new"}], True)]
    assert saved == [[{"api_key": "new", "merged": True}]]
    assert env == [{"API_KEY_ROUTING": "failover"}]
    assert FakeOpencode.API_KEY_ROUTING == "failover"
    assert rebuilt == [True]
    assert debug


def test_token_rejection():
    app, *_ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(app).get("/api/config/api-keys").status_code == 401
