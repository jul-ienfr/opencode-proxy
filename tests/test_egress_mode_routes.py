"""Egress mode/public proxy route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers import egress_mode_routes


class FakeWorker:
    port = 8899

    def is_alive(self):
        return True


class FakePool:
    def __init__(self):
        self._public_https_proxies = [
            {
                "host": "1.1.1.1",
                "port": 8080,
                "egress_ip": "9.9.9.9",
                "provider": "public",
                "last_status": 200,
                "last_checked_at": 12.5,
                "transport_url": "secret",
            }
        ]
        self._public_worker = FakeWorker()
        self.configured = []
        self.revalidated = 0

    def configure_public_proxies(self, config):
        self.configured.append(config)

    async def revalidate_public_proxies(self):
        self.revalidated += 1
        return 1


class FakeManager:
    def __init__(self):
        self.proxy_mode = "vpn"
        self.status = "connected"


def _app(*, token_error=None, pool=None, managers=None):
    pool = pool or FakePool()
    managers = managers if managers is not None else [FakeManager()]
    persisted = []
    audits = []
    debug = []
    settings = SimpleNamespace(
        IP_ROTATION={
            "public_proxies": {"enabled": False, "isolation": "process"},
            "egress_strategy": "fallback",
        }
    )
    app = FastAPI()
    egress_mode_routes.register_egress_mode_routes(
        app,
        token_checker=lambda request: token_error,
        pool_getter=lambda: pool,
        managers_getter=lambda: list(managers),
        settings=settings,
        persist_vpn_config=lambda updates: persisted.append(updates),
        egress_audit=lambda action, details: audits.append((action, details)),
        debug=debug.append,
    )
    return app, pool, managers, persisted, audits, debug


def test_public_proxies_read_hides_worker_url():
    app, *_ = _app()
    payload = TestClient(app).get("/api/vpn/public-proxies").json()
    assert payload["active"] == 1
    assert payload["worker"] == {"kind": "FakeWorker", "port": 8899, "alive": True}
    endpoint = payload["endpoints"][0]
    assert endpoint["egress_ip"] == "9.9.9.9"
    assert endpoint["transport"] == "worker"
    assert "transport_url" not in endpoint


def test_public_proxies_refresh_persists_and_configures():
    app, pool, _, persisted, *_ = _app()
    response = TestClient(app).post(
        "/api/vpn/public-proxies/refresh",
        json={"enabled": True, "max_active": 5},
    ).json()
    assert response == {"ok": True, "enabled": True, "isolation": "process", "active": 1}
    assert persisted[-1]["public_proxies"]["max_active"] == 5
    assert pool.configured[-1]["enabled"] is True


def test_public_proxies_refresh_no_endpoints():
    app, pool, *_ = _app()
    pool._public_https_proxies = []
    response = TestClient(app).post(
        "/api/vpn/public-proxies/refresh",
        json={"enabled": True},
    ).json()
    assert response["active"] == 0
    assert "aucun endpoint" in response["error"]


def test_public_proxies_revalidate():
    app, pool, *_ = _app()
    assert TestClient(app).post(
        "/api/vpn/public-proxies/revalidate"
    ).json() == {"ok": True, "before": 1, "active": 1}
    assert pool.revalidated == 1


def test_proxy_mode_direct_persists_and_audits():
    app, _, managers, persisted, audits, _ = _app()
    response = TestClient(app).post(
        "/api/vpn/proxy-mode",
        json={"mode": "direct"},
    ).json()
    assert response == {"ok": True, "mode": "direct"}
    assert managers[0].proxy_mode == "direct"
    assert persisted[-1]["proxy_mode"] == "direct"
    assert persisted[-1]["egress_providers"]
    assert audits[0][0] == "egress_mode"
    assert audits[0][1]["runtime_mode"] == "direct"


def test_proxy_mode_validation():
    app, *_ = _app()
    client = TestClient(app)
    assert "mode doit être" in client.post(
        "/api/vpn/proxy-mode",
        json={"mode": "nope"},
    ).json()["error"]
    assert client.post(
        "/api/vpn/proxy-mode",
        json={"providers": []},
    ).json()["error"] == "au moins un provider est requis"
    empty_app, *_ = _app(managers=[])
    assert TestClient(empty_app).post(
        "/api/vpn/proxy-mode",
        json={"mode": "vpn"},
    ).json()["error"] == "gestionnaire VPN non initialisé"


def test_public_proxy_token_rejection():
    app, *_ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(app).post(
        "/api/vpn/public-proxies/refresh",
        json={},
    ).status_code == 401
