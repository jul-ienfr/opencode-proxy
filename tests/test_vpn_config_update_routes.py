"""VPN configuration update route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import shared_state
from dashboard.routers import vpn_config_update_routes


class FakeManager:
    def __init__(self, station=1):
        self._station = station
        self.updated = []
        self.stacks = []
        self.config = {"enabled": True}

    async def update_config(self, body):
        self.updated.append(dict(body))

    async def set_stack(self, stack, propagate=True):
        self.stacks.append((stack, propagate))
        return {"ok": True}

    def get_config(self):
        return self.config


class FakePool:
    def __init__(self):
        self.updates = []

    def update_config(self, body):
        self.updates.append(dict(body))


def _app(*, token_error=None, write_error=None):
    writes = []
    persisted = []
    audits = []
    debug = []
    settings = SimpleNamespace(
        FREE_SUBSTITUTION_POLICIES=("homonym", "family")
    )

    def write_credentials(username, password):
        if write_error:
            raise ValueError(write_error)
        writes.append((username, password))

    app = FastAPI()
    vpn_config_update_routes.register_vpn_config_update_routes(
        app,
        token_checker=lambda request: token_error,
        settings=settings,
        write_credentials_env=write_credentials,
        persist_free_model_map=lambda mapping: persisted.append(mapping),
        quality_blocks_errors=lambda body: [],
        persist_vpn_config=lambda updates: persisted.append(updates),
        egress_audit=lambda action, details: audits.append((action, details)),
        debug=debug.append,
    )
    return app, writes, persisted, audits, debug


@pytest.fixture
def stations(monkeypatch):
    managers = [FakeManager(1), FakeManager(2)]
    pool = FakePool()
    monkeypatch.setattr(shared_state, "vpn_managers", managers, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    return managers, pool


def test_update_fans_out_and_persists_stack_last(stations):
    managers, pool = stations
    app, _, persisted, audits, _ = _app()
    response = TestClient(app).post(
        "/api/vpn-config",
        json={
            "max_free_attempts": 9,
            "auto_max_free_attempts": "yes",
            "station_bad_ttl_s": "30",
            "vpn_stack": "wireguard",
        },
    ).json()
    assert response == {"ok": True, "config": {"enabled": True}}
    assert managers[0].updated[0]["max_free_attempts"] == 3
    assert managers[0].updated[0]["auto_max_free_attempts"] is True
    assert managers[0].updated[0]["station_bad_ttl_s"] == 30
    assert managers[1].updated[0]["vpn_stack"] == "wireguard"
    assert pool.updates == managers[0].updated
    assert managers[0].stacks == [("wireguard", True)]
    assert managers[1].stacks == [("wireguard", False)]
    assert persisted == [{"max_free_attempts": 3, "auto_max_free_attempts": True, "station_bad_ttl_s": 30}, {"vpn_stack": "wireguard"}]
    assert audits == []


def test_credentials_write_and_error(stations):
    app, writes, *_ = _app()
    response = TestClient(app).post(
        "/api/vpn-config",
        json={"credentials": {"username": "u", "password": "p"}},
    )
    assert response.status_code == 200
    assert writes == [("u", "p")]

    error_app, _, _, _, _ = _app(write_error="bad credentials")
    response = TestClient(error_app).post(
        "/api/vpn-config",
        json={"credentials": {"username": "u", "password": "p"}},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "bad credentials"}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"max_free_attempts": "x"}, 200),
        ({"free_exception_fallback": "nope"}, 200),
        ({"free_substitution_policy": "nope"}, 400),
        ({"on_429_action": "nope"}, 400),
        ({"server_pick_mode": "nope"}, 400),
        ({"bad_ttl": "0"}, 400),
        ({"station_bad_ttl_s": "9999"}, 400),
        ({"latency_rotation": []}, 400),
        ({"latency_rotation": {"enabled": "yes"}}, 400),
        ({"free_parallel": []}, 200),
        ({"free_parallel_routing": "nope"}, 200),
        ({"free_parallel_mode": "nope"}, 200),
    ],
)
def test_validation_matrix(stations, body, status):
    app, *_ = _app()
    assert TestClient(app).post("/api/vpn-config", json=body).status_code == status


def test_station_count_without_manager_is_503(monkeypatch):
    monkeypatch.setattr(shared_state, "vpn_managers", [], raising=False)
    app, *_ = _app()
    response = TestClient(app).post(
        "/api/vpn-config",
        json={"station_count": 3},
    )
    assert response.status_code == 503
    assert "non initialisé" in response.json()["error"]


def test_station_count_out_of_range(stations):
    app, *_ = _app()
    response = TestClient(app).post(
        "/api/vpn-config",
        json={"station_count": 12},
    )
    assert response.status_code == 400
    assert "entre 1 et 10" in response.json()["error"]


def test_token_rejection(stations):
    app, *_ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(app).post("/api/vpn-config", json={}).status_code == 401
