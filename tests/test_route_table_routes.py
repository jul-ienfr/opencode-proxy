"""Route table projection tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.route_table_routes import register_route_table_routes


def _client():
    settings = SimpleNamespace(
        ROUTES={
            "sonnet": {
                "match": ["claude-sonnet*"],
                "model": "sonnet",
                "thinking": True,
                "geo": {"allowed": ["FR"]},
            },
            "haiku": {
                "match": ["claude-haiku*"],
                "model": "haiku",
            },
            "broken": {"model": "broken"},
        }
    )

    def resolve_geo(route):
        if route.get("model") == "broken":
            raise RuntimeError("bad geo")
        if route.get("model") == "haiku":
            return {
                "effective_allowed": {"FR", "DE"},
                "mode": "strict",
                "require_vpn": True,
                "geo_status": "degraded",
            }
        return {
            "effective_allowed": {"FR"},
            "mode": "compatible",
            "require_vpn": False,
            "geo_status": "ok",
        }

    settings.resolve_geo = resolve_geo
    app = FastAPI()
    register_route_table_routes(app, settings=settings)
    return TestClient(app)


def test_routes_enrich_and_paginate():
    payload = _client().get("/api/routes", params={"limit": 3}).json()
    assert payload["total"] == 3
    assert payload["limit"] == 3
    assert payload["has_more"] is False
    assert payload["routes"][0]["key"] == "sonnet"
    assert payload["routes"][0]["effective_allowed"] == ["FR"]
    assert payload["routes"][0]["mode"] == "compatible"
    assert payload["routes"][2]["geo_status"] == "ok"  # fallback


def test_routes_filters_search_and_geo_status():
    client = _client()
    assert client.get("/api/routes", params={"q": "haiku"}).json()["total"] == 1
    assert client.get(
        "/api/routes",
        params={"geo_status": "degraded"},
    ).json()["routes"][0]["key"] == "haiku"
    assert client.get(
        "/api/routes",
        params={"q": "SONNET"},
    ).json()["total"] == 1
    assert client.get(
        "/api/routes",
        params={"limit": 9999, "offset": 2},
    ).json()["limit"] == 200
