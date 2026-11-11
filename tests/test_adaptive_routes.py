"""Adaptive egress route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.adaptive_routes import register_adaptive_routes


class FakePool:
    def __init__(self, *, available=None, adaptive=None):
        self._available = list(available or [])
        self._egress_weights = {"warp": 60, "nordvpn": 40}
        self._egress_adaptive = adaptive or {}
        self._last_model = "deepseek-v4.1-flash-free"

    def _available_egress_providers(self, model=None):
        assert model == "deepseek-v4.1-flash-free"
        return list(self._available)

    def _adaptive_health(self, providers):
        return {
            "warp": {
                "ok": 2,
                "n429": 8,
                "recent_429": 4,
                "blocked_ratio": 0.4,
            },
            "nordvpn": {"ok": 10, "n429": 0, "recent_429": 0},
        }


def _client(pool):
    app = FastAPI()
    register_adaptive_routes(app, pool_getter=lambda: pool)
    return TestClient(app)


def test_adaptive_disabled_returns_base_weights():
    pool = FakePool(available=["warp", "nordvpn"])
    payload = _client(pool).get("/api/egress/adaptive").json()
    assert payload["enabled"] is False
    assert payload["available"] == ["warp", "nordvpn"]
    assert payload["base"] == {"warp": 60, "nordvpn": 40}
    assert payload["effective"] == payload["base"]


def test_adaptive_enabled_reweights_and_keeps_sum():
    pool = FakePool(
        available=["warp", "nordvpn"],
        adaptive={
            "enabled": True,
            "min_samples": 3,
            "recent_429_penalty": 0.25,
            "min_weight_pct": 2,
        },
    )
    payload = _client(pool).get("/api/egress/adaptive").json()
    assert payload["enabled"] is True
    assert payload["health"]["warp"]["recent_429"] == 4
    assert payload["effective"]["nordvpn"] > payload["base"]["nordvpn"]
    assert sum(payload["effective"].values()) == 100


def test_adaptive_without_pool_or_providers():
    assert _client(None).get("/api/egress/adaptive").json() == {
        "enabled": False,
        "available": [],
        "base": {},
        "health": {},
        "effective": {},
    }
    empty = FakePool(available=[])
    assert _client(empty).get("/api/egress/adaptive").json()["available"] == []
