"""VPN status projection tests (extracted router)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import shared_state
from dashboard.routers.vpn_status_routes import register_vpn_status_routes


class FakeCache:
    def __init__(self):
        self.data = {"ip_stats": {"1.2.3.4": 3}}

    def get(self, key):
        return self.data.get(key)


class FakePool:
    def get_status(self):
        return {
            "enabled": True,
            "status": "connected",
            "total": 1,
            "stations": [{"station": 1, "vpn_status": "connected"}],
        }


class FakeRotation:
    def get_status(self):
        return {"recent": ["1.1.1.1"]}


class FakeWatcher:
    def get_status(self):
        return {"alive": True}


class FakeManager:
    _station = 1
    status = "connected"
    enabled = True
    _disabled = False
    current_ip = "1.2.3.4"
    _current_country = "FR"
    _STATUS_CACHE_SECONDS = 30.0
    _last_status_refresh_at = time.monotonic()

    async def refresh_status(self, force=False):
        return self.get_status()

    def get_status(self):
        return {
            "station": 1,
            "status": "connected",
            "ip": "1.2.3.4",
            "server": "fr1",
            "current_country": "FR",
            "next_country": "DE",
            "country_pinned_at": 1.0,
            "country_rotation": True,
        }


def _client(monkeypatch, *, managers):
    async def ip_stats(_manager):
        return {}

    settings = SimpleNamespace(
        ENV_DIVERGENCE=[],
        IP_ROTATION={"enabled": True, "egress_providers": ["nordvpn"]},
        ROUTES={},
        GEO_ENABLED=False,
        GEO_VERSION=1,
        GEO_ALLOW_DIRECT_WHEN_COMPATIBLE=True,
        FREE_PARALLEL={"enabled": True},
        resolve_geo=lambda route: {"effective_allowed": set()},
    )
    monkeypatch.setattr(shared_state, "vpn_managers", managers, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", FakePool(), raising=False)
    monkeypatch.setattr(
        shared_state,
        "vpn_manager",
        managers[0] if managers else None,
        raising=False,
    )
    monkeypatch.setattr(
        shared_state,
        "shared_rotation",
        FakeRotation(),
        raising=False,
    )
    monkeypatch.setattr(
        shared_state,
        "docker_event_watcher",
        FakeWatcher(),
        raising=False,
    )
    app = FastAPI()
    register_vpn_status_routes(
        app,
        settings=settings,
        stats_cache=FakeCache(),
        ip_stats_db_getter=lambda: ip_stats,
        proxy_mode_getter=lambda: "vpn",
        socks5_payload=lambda pool: [],
        socks5_auto_rotate_state=lambda: True,
        config_yaml_mtime=lambda: 10.0,
        config_yaml_known_mtime_getter=lambda: 5.0,
    )
    return TestClient(app)


def test_vpn_status_without_manager(monkeypatch):
    payload = _client(monkeypatch, managers=[]).get("/api/vpn-status").json()
    assert payload["enabled"] is False
    assert payload["proxy_mode"] == "vpn"
    assert payload["config_yaml_dirty"] is True
    assert payload["socks5"] == {"proxies": [], "rotate": True}


def test_vpn_status_overlays_fleet_state(monkeypatch):
    payload = _client(
        monkeypatch,
        managers=[FakeManager()],
    ).get("/api/vpn-status").json()
    assert payload["status"] == "connected"
    assert payload["ip_stats"] == {"1.2.3.4": 3}
    assert payload["shared_rotation"] == {"recent": ["1.1.1.1"]}
    assert payload["watch_events"] == {"alive": True}
    assert payload["countries"]["1"]["current_country"] == "FR"
    assert payload["free_parallel_warning"] is None
    assert payload["egress_providers"] == ["nordvpn"]
