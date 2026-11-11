"""Configuration route tests (extracted router)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers import config_routes


class FakeSettings(SimpleNamespace):
    pass


class FakeManager:
    def __init__(self, running=True, port=4000):
        self.is_running = running
        self.port = port
        self.restarts = []

    def restart(self, **kwargs):
        self.restarts.append(kwargs)


def _app(*, token_error=None, manager=None, port=4000, host="0.0.0.0"):
    saved = []
    applied = []
    debug = []
    manager = manager or FakeManager()
    settings = FakeSettings(
        ROUTES={"sonnet": {"match": "claude-sonnet*", "model": "sonnet"}},
        FREE_MODEL_MAP={"x": "x-free"},
        FREE_PARALLEL={"n": 2},
        DISABLE_MAPPING=False,
        CUSTOM_ROUTES=[],
        OPENCODE_GO_WORKSPACE_ID="workspace-12345",
        OPENCODE_GO_AUTH_COOKIE="cookie-abcdef",
        get_free_substitution_policy=lambda: {"enabled": True},
    )
    app = FastAPI()
    config_routes.register_config_routes(
        app,
        token_checker=lambda request: token_error,
        settings=settings,
        get_models=lambda: [],
        local_ips_cache_getter=lambda: (time.monotonic(), ["10.0.0.1"]),
        get_local_ips=lambda: ["ignored"],
        default_port=port,
        proxy_getter=lambda: "http://proxy",
        api_key_getter=lambda: "abcdefghijkl",
        host_getter=lambda: host,
        api_keys_getter=lambda: [
            {
                "api_key": "abcdefghijkl",
                "go_workspace_id": "workspace-12345",
                "go_auth_cookie": "cookie-abcdef",
                "alias": "main",
            }
        ],
        api_key_routing_getter=lambda: "round-robin",
        model_limits=lambda models: {"limits": len(models)},
        model_capabilities=lambda models: {"caps": len(models)},
        save_env=lambda updates: saved.append(updates),
        apply_server_changes=lambda **kwargs: applied.append(kwargs),
        server_manager_getter=lambda: manager,
        debug=debug.append,
    )
    return app, saved, applied, debug, manager


def test_config_read_masks_secrets_and_reports_cache():
    app, *_ = _app()
    payload = TestClient(app).get("/api/config").json()
    assert payload["proxy"] == "http://proxy"
    assert payload["api_key_masked"] == "abcd****ijkl"
    assert payload["host"] == "0.0.0.0"
    assert payload["port"] == 4000
    assert payload["local_ips"] == ["10.0.0.1"]
    assert payload["routes"]["sonnet"]["model"] == "sonnet"
    assert payload["model_limits"] == {"limits": 0}
    assert payload["model_capabilities"] == {"caps": 0}
    assert payload["proxy_running"] is True
    assert payload["go_workspace_id_masked"] == "work****"
    assert payload["go_auth_cookie_masked"] == "cookie****"
    assert payload["api_keys"][0]["api_key_masked"] == "abcd****ijkl"
    assert payload["api_keys"][0]["alias"] == "main"
    assert payload["free_model_map_effective"] == {"x": "x-free"}
    assert payload["routing"] == "round-robin"


def test_config_read_rejects_bad_token():
    app, *_ = _app(token_error=JSONResponse({"error": "token"}, status_code=401))
    assert TestClient(app).get("/api/config").status_code == 401


def test_config_read_refreshes_stale_proxy_flag():
    """[fix 10/11] proxy_running doit refléter l'état réel, pas un flag périmé."""

    class HealingManager(FakeManager):
        def __init__(self):
            super().__init__(running=False, port=4000)

        def refresh_state(self):
            self.is_running = True
            return True

    app, *_ = _app(manager=HealingManager())
    assert TestClient(app).get("/api/config").json()["proxy_running"] is True


def test_config_update_persists_env_and_restarts(monkeypatch):
    app, saved, applied, _debug, manager = _app()
    threads = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            threads.append((self.name, self.daemon))
            self.target()

    monkeypatch.setattr(config_routes.threading, "Thread", FakeThread)
    client = TestClient(app)
    response = client.post(
        "/api/config",
        json={
            "routes": {"opus": "o", "sonnet": "s", "haiku": "h"},
            "proxy": "http://new",
            "disable_mapping": True,
            "port": 4100,
            "host": "127.0.0.1",
        },
    ).json()
    assert response["status"] == "ok"
    assert "Redémarrage" in response["message"]
    assert saved[-1]["OPUS_MAP_MODEL"] == "o"
    assert saved[-1]["OPENCODE_PROXY"] == "http://new"
    assert saved[-1]["DISABLE_MAPPING"] == "1"
    assert saved[-1]["OPENCODE_PORT"] == "4100"
    assert saved[-1]["OPENCODE_HOST"] == "127.0.0.1"
    assert applied == [{"port": 4100, "host": "127.0.0.1"}]
    assert manager.restarts == [{"port": 4100, "host": "127.0.0.1"}]
    assert threads == [("proxy-restart", False)]


def test_config_update_rejects_invalid_port_and_noop_restart():
    app, saved, applied, *_ = _app()
    client = TestClient(app)
    assert client.post("/api/config", json={"port": "nope"}).json() == {
        "status": "error",
        "message": "Valeur de port invalide",
    }
    assert client.post("/api/config", json={"port": 70000}).json()["status"] == "error"
    assert saved == []
    assert applied == []
    payload = client.post("/api/config", json={"port": 4000, "host": "0.0.0.0"}).json()
    assert payload["needs_restart"] is False
    assert payload["message"] == "Configuration mise à jour."
