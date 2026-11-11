"""Egress capacity route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.capacity_routes import register_capacity_routes


class FakeReport:
    def to_dict(self):
        return {"target_slots": 6, "reserve_policy": "full", "providers": []}


def _app(*, token_error=None, persist_error=None):
    persisted: list[dict] = []
    audited: list[tuple] = []

    def persist(updates):
        persisted.append(dict(updates))
        return persist_error

    app = FastAPI()
    register_capacity_routes(
        app,
        config_getter=lambda: {
            "proxy_mode": "vpn",
            "egress_capacity": {"target_slots": 6, "reserve_policy": "full"},
        },
        capacity_counts_getter=lambda: {},
        observed_counts_getter=lambda: {},
        endpoint_observed_getter=lambda: {},
        endpoint_inventory_getter=lambda: [{"provider": "warp"}],
        token_checker=lambda request: token_error,
        persist_config=persist,
        audit=lambda name, payload: audited.append((name, payload)),
    )
    return app, persisted, audited


def test_get_capacity_returns_advisor_and_endpoints(monkeypatch):
    monkeypatch.setattr(
        "free.capacity.evaluate_capacity",
        lambda **kwargs: FakeReport(),
    )
    app, _, _ = _app()
    payload = TestClient(app).get("/api/egress/capacity").json()
    assert payload["target_slots"] == 6
    assert payload["endpoints"] == [{"provider": "warp"}]


def test_post_capacity_validates_and_persists():
    app, persisted, audited = _app()
    client = TestClient(app)
    invalid = client.post("/api/egress/capacity", json={"target_slots": 99})
    assert "1 et 10" in invalid.json()["error"]
    invalid_policy = client.post(
        "/api/egress/capacity",
        json={"reserve_policy": "unknown"},
    )
    assert "full|shared|proportional" in invalid_policy.json()["error"]
    empty = client.post("/api/egress/capacity", json={})
    assert "requis" in empty.json()["error"]
    ok = client.post(
        "/api/egress/capacity",
        json={"target_slots": 6, "reserve_policy": "shared"},
    )
    assert ok.json()["ok"] is True
    assert persisted == [
        {"egress_capacity": {"target_slots": 6, "reserve_policy": "shared"}}
    ]
    assert audited[0][0] == "capacity_config"


def test_post_capacity_token_and_persist_errors():
    token_app, persisted, _ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    response = TestClient(token_app).post(
        "/api/egress/capacity",
        json={"target_slots": 6},
    )
    assert response.status_code == 401
    assert persisted == []
    persist_app, _, _ = _app(persist_error="lecture seule")
    response = TestClient(persist_app).post(
        "/api/egress/capacity",
        json={"target_slots": 6},
    )
    assert response.json()["error"] == "lecture seule"
