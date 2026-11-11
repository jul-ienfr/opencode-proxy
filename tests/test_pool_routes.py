"""Pool and latency status route tests (extracted router)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.pool_routes import register_pool_routes


class FakePool:
    def get_status(self):
        return {"stations": 6, "healthy": 5}


class FakeTracker:
    def snapshot(self):
        return SimpleNamespace(
            count=4,
            ewma_ms=21.5,
            p95_ms=30.0,
            consecutive_slow=1,
        )


def _engine():
    return SimpleNamespace(
        cfg=SimpleNamespace(
            enabled=True,
            slow_threshold_ms=800,
            ewma_threshold_ms=600,
            p95_threshold_ms=900,
            floor_ms=200,
            consecutive_slow=2,
            soft_cooldown_sec=60,
            hard_cooldown_sec=300,
            per_model={"m": 700},
            stream_metric="ttfb",
        ),
        _cooldowns={(1, "1.1.1.1"): ("soft", time.time() + 60.0)},
        _soft_history={(2, "2.2.2.2")},
        _trackers={(1, "1.1.1.1"): FakeTracker()},
    )


def _app(*, pool=None, engine_getter=None):
    app = FastAPI()
    register_pool_routes(
        app,
        pool_getter=lambda: pool,
        latency_engine_getter=engine_getter or _engine,
    )
    return app


def test_routes_are_registered():
    paths = {route.path for route in _app().routes}
    assert "/api/pool-status" in paths
    assert "/api/latency-status" in paths


def test_pool_status_is_fail_soft_when_uninitialized():
    response = TestClient(_app(pool=None)).get("/api/pool-status")
    assert response.status_code == 503
    assert response.json()["proxy_mode"] == "none"


def test_pool_status_returns_pool_view():
    payload = TestClient(_app(pool=FakePool())).get("/api/pool-status").json()
    assert payload == {"stations": 6, "healthy": 5}


def test_latency_status_exposes_engine_state():
    payload = TestClient(_app()).get("/api/latency-status").json()
    assert payload["enabled"] is True
    assert payload["config"]["slow_threshold_ms_per_model"] == {"m": 700}
    assert payload["cooldowns"][0]["kind"] == "soft"
    assert payload["soft_history"] == ["2|2.2.2.2"]
    assert payload["trackers"][0]["ewma_ms"] == 21.5


def test_latency_engine_failure_is_fail_soft():
    def broken():
        raise RuntimeError("no engine")

    payload = TestClient(_app(engine_getter=broken)).get("/api/latency-status").json()
    assert payload["enabled"] is False
    assert "no engine" in payload["error"]
