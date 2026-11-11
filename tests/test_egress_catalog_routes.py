"""Egress catalog read-route tests (extracted router)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.egress_catalog_routes import register_egress_catalog_routes


@dataclass
class Option:
    key: str
    provider: str
    status: str


class FakeStore:
    def summary(self):
        return {"nordvpn": {"usable": 1}}

    def list_runs(self, provider=None, limit=20):
        return [{"run_id": "r1", "provider": provider or "nordvpn"}]

    def list_options(self, provider=None, status=None, kind=None, limit=100, offset=0):
        return [Option("k", provider or "nordvpn", status or "usable")]

    def list_observations(self, key=None, limit=50):
        return [{"key": key or "k"}]

    def list_sources(self):
        return [{"id": "s1"}]


def _app(runtime):
    app = FastAPI()
    register_egress_catalog_routes(app, lambda: runtime)
    return app


def test_routes_are_registered():
    app = _app(None)
    paths = {route.path for route in app.routes}
    assert "/api/egress/catalog" in paths
    assert "/api/egress/catalog/options" in paths
    assert "/api/egress/catalog/runs" in paths
    assert "/api/egress/catalog/observations" in paths
    assert "/api/egress/catalog/sources" in paths


def test_disabled_catalog_is_fail_soft():
    client = TestClient(_app(None))
    payload = client.get("/api/egress/catalog").json()
    assert payload["enabled"] is False
    assert client.get("/api/egress/catalog/options").json() == {"options": []}
    assert client.get("/api/egress/catalog/runs").json() == {"runs": []}


def test_enabled_catalog_reads():
    runtime = SimpleNamespace(store=FakeStore(), threat_intel=None)
    client = TestClient(_app(runtime))
    summary = client.get("/api/egress/catalog").json()
    assert summary["enabled"] is True
    assert summary["providers"]["nordvpn"]["usable"] == 1
    options = client.get("/api/egress/catalog/options", params={"provider": "nordvpn"}).json()
    assert options["options"][0]["key"] == "k"
    runs = client.get("/api/egress/catalog/runs", params={"provider": "nordvpn"}).json()
    assert runs["runs"][0]["run_id"] == "r1"
    observations = client.get("/api/egress/catalog/observations", params={"key": "k"}).json()
    assert observations["observations"][0]["key"] == "k"
    sources = client.get("/api/egress/catalog/sources").json()
    assert sources["sources"][0]["id"] == "s1"
