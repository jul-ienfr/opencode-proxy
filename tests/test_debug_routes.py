"""Debug/log route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.debug_routes import register_debug_routes


def _app(tmp_path, *, lines=None):
    settings = SimpleNamespace(DEBUG=False)
    saved = []
    configured = []
    debug = []
    path = tmp_path / "logs" / "debug.log"
    path.parent.mkdir()
    path.write_text("\n".join(lines or ["one", "two", "three"]) + "\n", encoding="utf-8")
    app = FastAPI()
    register_debug_routes(
        app,
        settings=settings,
        log_lines_getter=lambda: list(lines or ["one", "two", "three"]),
        save_env=lambda updates: saved.append(updates),
        debug_log_path=str(path),
        set_debug_log_file=configured.append,
        debug=debug.append,
    )
    return app, settings, saved, configured, debug, path


def test_runtime_logs_pagination(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/api/logs", params={"limit": 2}).json() == {
        "logs": ["one", "two"],
        "total": 3,
        "has_more": True,
    }
    assert client.get(
        "/api/logs",
        params={"limit": 9999, "offset": 2},
    ).json()["has_more"] is False


def test_debug_toggle(tmp_path):
    app, settings, saved, configured, debug, _ = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/api/debug").json() == {"enabled": False}
    assert client.post("/api/debug", json={"enabled": True}).json() == {
        "enabled": True
    }
    assert settings.DEBUG is True
    assert saved == [{"OPENCODE_DEBUG": "1"}]
    assert configured
    assert debug[-1] == "Debug mode ENABLED via API"
    client.post("/api/debug", json={"enabled": False})
    assert saved[-1] == {"OPENCODE_DEBUG": "0"}
    assert debug[-1] == "Debug mode DISABLED via API"


def test_debug_logs_reversed_cached_and_clear(tmp_path):
    app, _, _, _, debug, path = _app(tmp_path)
    client = TestClient(app)
    payload = client.get("/api/debug/logs", params={"limit": 2}).json()
    assert payload == {
        "logs": ["three", "two"],
        "total": 3,
        "has_more": True,
    }
    cached = client.get("/api/debug/logs", params={"limit": 2}).json()
    assert cached == payload
    assert client.delete("/api/debug/logs").json() == {
        "status": "ok",
        "message": "Debug log cleared.",
    }
    assert path.read_text(encoding="utf-8") == ""
    assert debug[-1] == "Debug log cleared via API"


def test_debug_logs_missing_file(tmp_path):
    app, *_ = _app(tmp_path)
    path = tmp_path / "missing.log"
    app2 = FastAPI()
    register_debug_routes(
        app2,
        settings=SimpleNamespace(DEBUG=False),
        log_lines_getter=list,
        save_env=lambda updates: None,
        debug_log_path=str(path),
        set_debug_log_file=lambda path: None,
        debug=lambda message: None,
    )
    assert TestClient(app2).get("/api/debug/logs").json() == {
        "logs": [],
        "total": 0,
        "has_more": False,
    }
