"""Quota snapshot route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.quota_routes import register_quota_routes


def _app(state="fresh", age=12.3, data=None):
    async def snapshot():
        return data or {"workspaces": [{"id": "w1"}]}

    app = FastAPI()
    register_quota_routes(
        app,
        snapshot_getter=snapshot,
        cache_state_getter=lambda: (state, age),
        max_age_s=30,
        stale_s=120,
    )
    return app


def test_fresh_snapshot_headers():
    response = TestClient(_app("fresh", 12.3)).get("/api/quotas")
    assert response.status_code == 200
    assert response.json()["workspaces"][0]["id"] == "w1"
    assert response.headers["x-cache"] == "HIT"
    assert response.headers["x-cache-age"] == "12.3"
    assert "max-age=30" in response.headers["cache-control"]
    assert "stale-while-revalidate=120" in response.headers["cache-control"]


def test_stale_and_unknown_age():
    response = TestClient(_app("stale", float("inf"))).get("/api/quotas")
    assert response.headers["x-cache"] == "STALE"
    assert response.headers["x-cache-age"] == "unknown"


def test_miss_state():
    response = TestClient(_app("miss", 0.0)).get("/api/quotas")
    assert response.headers["x-cache"] == "MISS"
    assert response.headers["x-cache-age"] == "0.0"
