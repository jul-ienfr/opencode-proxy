"""Egress catalog mutation-route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.egress_catalog_mutations import (
    register_egress_catalog_mutations,
)


class FakeStore:
    def upsert_source(self, body):
        return {"id": body["id"], "type": body["type"]}


class FakeThreat:
    def __init__(self, errors=None):
        self.errors = list(errors or [])

    def refresh_once(self):
        return {"errors": self.errors, "updated": 5}


class FakeRuntime:
    policies = {"public", "warp"}

    def __init__(self, threat_errors=None):
        self.store = FakeStore()
        self.threat_intel = FakeThreat(threat_errors)
        self.synced = []

    async def refresh_now(self, provider):
        return SimpleNamespace(
            errors=[],
            candidates=3,
            promoted=1,
            quarantined=0,
            dead=0,
            skipped=0,
        )

    def block_indicator(self, ip):
        if ip == "invalid":
            raise ValueError("ip invalide")
        return {"ip": ip, "blocked": 1}

    def sync_public_pool(self, _):
        self.synced.append(True)


def _app(runtime, *, token_error=None):
    app = FastAPI()
    denylist: list[str] = []

    register_egress_catalog_mutations(
        app,
        catalog_getter=lambda: runtime,
        token_checker=lambda request: token_error,
        denylist_getter=lambda: list(denylist),
        denylist_setter=lambda value: denylist.__setitem__(slice(None), value),
    )
    return app, denylist


def test_routes_are_registered():
    app, _ = _app(None)
    paths = {route.path for route in app.routes}
    assert "/api/egress/catalog/refresh" in paths
    assert "/api/egress/catalog/threat-feeds/refresh" in paths
    assert "/api/egress/catalog/block" in paths
    assert "/api/egress/catalog/sources" in paths


def test_token_rejection_is_returned_as_is():
    app, _ = _app(FakeRuntime(), token_error=JSONResponse({"error": "token"}, status_code=401))
    client = TestClient(app)
    assert client.post("/api/egress/catalog/refresh", json={}).status_code == 401


def test_refresh_disabled_returns_503():
    app, _ = _app(None)
    client = TestClient(app)
    response = client.post("/api/egress/catalog/refresh", json={"provider": "public"})
    assert response.status_code == 503
    assert "désactivé" in response.json()["error"]


def test_refresh_rejects_inactive_provider():
    app, _ = _app(FakeRuntime())
    client = TestClient(app)
    response = client.post("/api/egress/catalog/refresh", json={"provider": "unknown"})
    assert response.status_code == 400
    assert "provider inactif" in response.json()["error"]


def test_refresh_success_returns_202_payload():
    app, _ = _app(FakeRuntime())
    client = TestClient(app)
    response = client.post("/api/egress/catalog/refresh", json={"provider": "warp"})
    assert response.status_code == 202
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "warp"
    assert payload["candidates"] == 3
    assert payload["promoted"] == 1


def test_threat_feeds_disabled_returns_503():
    runtime = FakeRuntime()
    runtime.threat_intel = None
    app, _ = _app(runtime)
    client = TestClient(app)
    response = client.post("/api/egress/catalog/threat-feeds/refresh", json={})
    assert response.status_code == 503


def test_threat_feeds_refresh_reports_errors():
    app, _ = _app(FakeRuntime(threat_errors=["feed down"]))
    client = TestClient(app)
    payload = client.post("/api/egress/catalog/threat-feeds/refresh", json={}).json()
    assert payload["ok"] is False
    assert payload["errors"] == ["feed down"]
    assert payload["updated"] == 5


def test_block_persists_denylist_once():
    runtime = FakeRuntime()
    app, denylist = _app(runtime)
    client = TestClient(app)
    first = client.post("/api/egress/catalog/block", json={"ip": "1.2.3.4"})
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert denylist == ["1.2.3.4"]
    second = client.post("/api/egress/catalog/block", json={"ip": "1.2.3.4"})
    assert second.status_code == 200
    assert denylist == ["1.2.3.4"]


def test_block_value_error_returns_400():
    app, denylist = _app(FakeRuntime())
    client = TestClient(app)
    response = client.post("/api/egress/catalog/block", json={"ip": "invalid"})
    assert response.status_code == 400
    assert "ValueError" in response.json()["error"]
    assert denylist == []


def test_source_upsert_validates_and_stores():
    app, _ = _app(FakeRuntime())
    client = TestClient(app)
    response = client.post(
        "/api/egress/catalog/sources",
        json={"id": "warp-main", "type": "warp"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "source": {"id": "warp-main", "type": "warp"},
    }


def test_source_upsert_rejects_invalid_type():
    app, _ = _app(FakeRuntime())
    client = TestClient(app)
    response = client.post(
        "/api/egress/catalog/sources",
        json={"id": "broken", "type": "unknown"},
    )
    assert response.status_code == 400
    assert "ValueError" in response.json()["error"]
