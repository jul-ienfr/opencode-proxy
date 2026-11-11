"""Free-leg registry/allocator status route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.free_routes import register_free_status_routes

STATUS = {
    "generated_at": "2026-11-09T00:00:00+00:00",
    "enabled": True,
    "allocator": {"chains": 3},
    "db": {
        "assignments_open": 2,
        "open_assignments": [{"id": "a1"}],
        "ip_state": {"usable": 12},
        "extra": "ignored",
    },
    "sources": [{"id": "warp", "adapter": "no-adapter"}],
}


def _app(status=None, *, token_error=None):
    app = FastAPI()
    register_free_status_routes(
        app,
        token_checker=lambda request: token_error,
        registry_status_getter=lambda: dict(status or STATUS),
    )
    return app


def test_routes_are_registered():
    paths = {route.path for route in _app().routes}
    assert "/api/free/allocator/status" in paths
    assert "/api/free/ip-registry/status" in paths


def test_token_rejection_is_returned_as_is():
    client = TestClient(_app(token_error=JSONResponse({"error": "token"}, status_code=401)))
    assert client.get("/api/free/allocator/status").status_code == 401
    assert client.get("/api/free/ip-registry/status").status_code == 401


def test_allocator_status_is_projected():
    payload = TestClient(_app()).get("/api/free/allocator/status").json()
    assert payload["enabled"] is True
    assert payload["allocator"] == {"chains": 3}
    assert payload["db"] == {
        "assignments_open": 2,
        "open_assignments": [{"id": "a1"}],
        "ip_state": {"usable": 12},
    }


def test_ip_registry_status_is_complete():
    payload = TestClient(_app()).get("/api/free/ip-registry/status").json()
    assert payload["sources"] == [{"id": "warp", "adapter": "no-adapter"}]


def test_missing_db_section_is_fail_soft():
    status = {"enabled": False, "allocator": None, "db": None}
    payload = TestClient(_app(status)).get("/api/free/allocator/status").json()
    assert payload["db"] == {
        "assignments_open": 0,
        "open_assignments": [],
        "ip_state": {},
    }
