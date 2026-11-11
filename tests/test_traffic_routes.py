"""Traffic route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers import traffic_routes


class FakeCapture:
    def __init__(self):
        self.frames_data = [{"id": 1}, {"id": 2}]
        self.detail = {1: {"id": 1, "path": "/x"}}
        self.config = []
        self.cleared = 0
        self.stats_calls = []

    def status(self):
        return {"enabled": True, "frames": 2}

    def configure(self, **kwargs):
        self.config.append(kwargs)

    def frames(self, **kwargs):
        return self.frames_data[: kwargs["limit"]]

    def frame_detail(self, frame_id):
        return self.detail.get(frame_id)

    def clear(self):
        self.cleared += 1
        return 2

    def stats(self, window=60.0):
        self.stats_calls.append(window)
        return {"window": window}


def _app(*, token_error=None):
    capture = FakeCapture()
    state = {"enabled": None, "viewer": 0, "lazy": 0}
    debug = []

    def mark_viewer():
        state["viewer"] += 1

    def apply_lazy():
        state["lazy"] += 1
        if state["enabled"] is None:
            state["enabled"] = True

    def setter(value):
        state["enabled"] = value

    app = FastAPI()
    traffic_routes.register_traffic_routes(
        app,
        token_checker=lambda request: token_error,
        capture=capture,
        mark_viewer=mark_viewer,
        apply_lazy=apply_lazy,
        user_enabled_getter=lambda: state["enabled"],
        user_enabled_setter=setter,
        debug=debug.append,
    )
    return app, capture, state, debug


def test_traffic_status_initializes_lazy_state():
    app, _, state, _ = _app()
    payload = TestClient(app).get("/api/traffic/status").json()
    assert payload == {"enabled": True, "frames": 2, "user_enabled": True}
    assert state["viewer"] == 1
    assert state["lazy"] == 1


def test_traffic_config_updates_user_and_capture():
    app, capture, state, debug = _app()
    payload = TestClient(app).post(
        "/api/traffic/config",
        json={"enabled": False, "max_frames": 10},
    ).json()
    assert payload["ok"] is True
    assert state["enabled"] is False
    assert state["lazy"] == 1
    assert capture.config == [{"max_frames": 10, "body_cap": None, "max_bytes": None}]
    assert debug[-1].startswith("  [traffic] config updated")
    TestClient(app).post("/api/traffic/config", json={"max_frames": 5})
    assert state["viewer"] == 1


def test_traffic_frames_detail_clear_stats():
    app, capture, state, _ = _app()
    client = TestClient(app)
    frames = client.get(
        "/api/traffic/frames",
        params={"limit": 1, "offset": 0},
    ).json()
    assert frames["frames"] == [{"id": 1}]
    assert frames["total"] == 1
    assert frames["status"]["enabled"] is True
    assert client.get("/api/traffic/frames/1").json()["path"] == "/x"
    assert client.get("/api/traffic/frames/9").status_code == 404
    assert client.post("/api/traffic/clear").json() == {"ok": True, "cleared": 2}
    assert capture.cleared == 1
    assert client.get("/api/traffic/stats", params={"window": 0}).json() == {
        "window": 1.0
    }
    assert capture.stats_calls == [1.0]
    assert state["viewer"] >= 4


def test_traffic_token_rejection():
    app, capture, state, _ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(app).get("/api/traffic/status").status_code == 401
    assert state["viewer"] == 0
    assert capture.config == []
