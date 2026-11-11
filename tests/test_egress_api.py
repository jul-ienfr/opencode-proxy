"""Egress catalog API: summary, options, runs, sources, refresh."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import dashboard.api as api
import shared_state
from dashboard.api import register_dashboard
from free.egress_adapters.base import RefreshResult
from free.egress_catalog import Observation, ObservationPhase, Provider, ProviderPolicy, new_option
from free.egress_store import EgressStore


class _Runtime:
    def __init__(self, store: EgressStore):
        self.store = store
        self.policies = {Provider.PUBLIC: ProviderPolicy(provider=Provider.PUBLIC)}
        self.calls = []
        self.threat_intel = None

    async def refresh_now(self, provider: str):
        self.calls.append(provider)
        return RefreshResult(provider=provider, candidates=2, promoted=1, quarantined=1)

    def rotation_plan(self):
        return {"public": {"block": "1.2.3.4", "score": 42.0, "status": "usable"}}

    def block_indicator(self, ip: str):
        return {"ip": ip, "matched": 1, "denylist": [ip]}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    register_dashboard(fast, str(tmp_path), sqlite3.connect(":memory:"))
    store = EgressStore(tmp_path / "registry.db")
    runtime = _Runtime(store)
    monkeypatch.setattr(shared_state, "egress_catalog_runtime", runtime, raising=False)
    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "", raising=False)
    yield fast, store, runtime
    store.close()


def _seed(store: EgressStore) -> None:
    policy = ProviderPolicy(provider=Provider.PUBLIC, ttl_usable_s=10_000.0)
    option = new_option(
        provider=Provider.PUBLIC,
        kind="https",
        host="1.2.3.4",
        port=443,
        egress_ip="1.2.3.4",
    )
    store.apply(
        option,
        Observation(
            phase=ObservationPhase.FREE_GATE,
            status=200,
            result="usable",
            observed_at=__import__("time").time(),
        ),
        policy,
    )


def test_summary_endpoint_reports_per_provider(ctx):
    fast, store, _runtime = ctx
    _seed(store)
    with TestClient(fast) as client:
        payload = client.get("/api/egress/catalog").json()
    assert payload["enabled"] is True
    assert payload["providers"][Provider.PUBLIC]["usable"] == 1
    assert payload["rotation"][Provider.PUBLIC]["block"] == "1.2.3.4"


def test_options_never_expose_the_worker_transport_url(ctx):
    fast, store, _runtime = ctx
    _seed(store)
    with TestClient(fast) as client:
        payload = client.get("/api/egress/catalog/options?provider=public&status=usable").json()
    assert len(payload["options"]) == 1
    assert "transport_url" not in payload["options"][0]
    assert payload["options"][0]["egress_ip"] == "1.2.3.4"
    assert payload["options"][0]["score"] > 0


def test_options_filter_by_kind(ctx):
    fast, store, _runtime = ctx
    _seed(store)
    from free.egress_catalog import Observation, ObservationPhase, ProviderPolicy, new_option

    store.apply(
        new_option(
            provider=Provider.PUBLIC,
            kind="socks5",
            host="5.6.7.8",
            port=1080,
            egress_ip="5.6.7.8",
        ),
        Observation(
            phase=ObservationPhase.FREE_GATE,
            status=200,
            result="usable",
            observed_at=__import__("time").time(),
        ),
        ProviderPolicy(provider=Provider.PUBLIC, ttl_usable_s=10_000.0),
    )
    with TestClient(fast) as client:
        payload = client.get("/api/egress/catalog/options?kind=socks5").json()
    assert [option["kind"] for option in payload["options"]] == ["socks5"]


def test_runs_endpoint_is_read_only(ctx):
    fast, store, _runtime = ctx
    run_id = store.start_run(Provider.PUBLIC)
    store.finish_run(run_id, status="ok", candidates=1, promoted=1)
    with TestClient(fast) as client:
        payload = client.get("/api/egress/catalog/runs").json()
    assert payload["runs"][0]["status"] == "ok"


def test_observations_endpoint_exposes_the_raw_proof(ctx):
    fast, store, _runtime = ctx
    _seed(store)
    option = store.list_options(status="usable")[0]
    with TestClient(fast) as client:
        payload = client.get(f"/api/egress/catalog/observations?key={option.key}").json()
    assert len(payload["observations"]) == 1
    assert payload["observations"][0]["status"] == 200
    assert payload["observations"][0]["result"] == "usable"


def test_refresh_endpoint_returns_202_and_calls_the_runtime(ctx):
    fast, _store, runtime = ctx
    with TestClient(fast) as client:
        response = client.post("/api/egress/catalog/refresh", json={"provider": "public"})
    assert response.status_code == 202
    assert response.json()["promoted"] == 1
    assert runtime.calls == ["public"]


def test_refresh_unknown_provider_is_rejected(ctx):
    fast, _store, _runtime = ctx
    with TestClient(fast) as client:
        response = client.post("/api/egress/catalog/refresh", json={"provider": "warp"})
    assert response.status_code == 400


def test_source_upsert_is_persisted(ctx):
    fast, store, _runtime = ctx
    with TestClient(fast) as client:
        response = client.post(
            "/api/egress/catalog/sources",
            json={
                "id": "new-list",
                "type": "public_text",
                "url": "https://example.test/list.txt",
                "enabled": True,
            },
        )
    assert response.status_code == 200
    assert store.get_source("new-list") is not None


def test_block_endpoint_persists_the_denylist(ctx, monkeypatch):
    fast, _store, _runtime = ctx
    saved = []
    monkeypatch.setattr(api.config_settings, "yaml_get", lambda *a, **k: ["1.1.1.1/32"])
    monkeypatch.setattr(api.config_settings, "yaml_set", lambda section, key, value: saved.append(value))
    with TestClient(fast) as client:
        response = client.post("/api/egress/catalog/block", json={"ip": "2.2.2.2"})
    assert response.status_code == 200
    assert saved == [["1.1.1.1/32", "2.2.2.2"]]


def test_block_endpoint_rejects_an_invalid_ip(ctx, monkeypatch):
    fast, _store, runtime = ctx

    def _broken(_ip):
        raise ValueError("invalid")

    monkeypatch.setattr(runtime, "block_indicator", _broken)
    with TestClient(fast) as client:
        response = client.post("/api/egress/catalog/block", json={"ip": "not-an-ip"})
    assert response.status_code == 400


def test_threat_feed_refresh_is_unavailable_when_disabled(ctx):
    fast, _store, _runtime = ctx
    with TestClient(fast) as client:
        response = client.post("/api/egress/catalog/threat-feeds/refresh")
    assert response.status_code == 503


def test_threat_feed_refresh_updates_the_runtime(ctx):
    fast, _store, runtime = ctx
    runtime.threat_intel = SimpleNamespace(
        refresh_once=lambda: {"counts": {"feed-a": 3}, "errors": [], "stats": {"exact": 3, "networks": 0}}
    )
    with TestClient(fast) as client:
        response = client.post("/api/egress/catalog/threat-feeds/refresh")
    assert response.status_code == 200
    assert response.json()["counts"]["feed-a"] == 3


def test_disabled_catalog_returns_an_empty_view(ctx, monkeypatch):
    fast, _store, _runtime = ctx
    monkeypatch.setattr(shared_state, "egress_catalog_runtime", None, raising=False)
    with TestClient(fast) as client:
        payload = client.get("/api/egress/catalog").json()
    assert payload == {"enabled": False, "providers": {}, "last_run": None}
