"""SOCKS5/HTTPS proxy backend route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers import proxy_backend_routes


class FakeHealth:
    def __init__(self):
        self.repairs = 0
        self.observed = []

    def get(self, station):
        return self

    def repair_succeeded(self):
        self.repairs += 1

    def observe(self, station, ok):
        self.observed.append((station, ok))


class FakePool:
    def __init__(self):
        self._socks5_proxies = [
            {"host": "1.1.1.1", "port": 1080, "label": "lab", "enabled": True}
        ]
        self._https_proxies = [{"host": "2.2.2.2", "port": 89, "enabled": True}]
        self._socks5_auto_rotate = False
        self._socks5_fallback_enabled = False
        self._https_fallback_enabled = False
        self.proxy_mode = "socks5"
        self.enabled = True
        self.refreshed = []
        self.rotated_managed = 0
        self.rotated_now = 0
        self._station_health = FakeHealth()
        self._socks5_enabled_eps = lambda: [
            SimpleNamespace(label="lab", port=1080, _station=3)
        ]

    def refresh_socks5_egress(self, force=False):
        self.refreshed.append(force)
        return {"ep": "ok"}

    def duplicate_egress_groups(self):
        return {"dup": 1}

    def rotate_exhausted_managed(self):
        self.rotated_managed += 1
        return 2

    def rotation_scan(self):
        return [{"station": 1}]

    def rotate_socks5_now(self):
        self.rotated_now += 1
        return SimpleNamespace(pid="proxy-1")


def _app(*, token_error=None, pool=None):
    pool = pool or FakePool()
    applied_socks5 = []
    applied_https = []
    persisted = []
    probes = []

    def apply_socks5(rows, action="socks5_update"):
        applied_socks5.append((rows, action))
        pool._socks5_proxies = list(rows)
        return None

    def apply_https(rows):
        applied_https.append(rows)
        pool._https_proxies = list(rows)
        return None

    async def socks5_probe(host, port, **kwargs):
        probes.append(("socks5", host, port, kwargs))
        return {"ok": True, "opencode_ok": True}

    async def https_probe(host, port, **kwargs):
        probes.append(("https", host, port, kwargs))
        return {"ok": True}

    app = FastAPI()
    proxy_backend_routes.register_proxy_backend_routes(
        app,
        token_checker=lambda request: token_error,
        pool_getter=lambda: pool,
        socks5_payload=lambda target: list(target._socks5_proxies),
        socks5_auto_rotate_state=lambda: pool._socks5_auto_rotate,
        socks5_fallback_state=lambda: pool._socks5_fallback_enabled,
        apply_socks5_rows=apply_socks5,
        as_index=lambda value: int(value),
        stored_proxy_credentials=lambda *args: ("u", "p"),
        socks5_probe=socks5_probe,
        https_fallback_state=lambda: pool._https_fallback_enabled,
        https_payload=lambda target: list(target._https_proxies),
        https_manual_rows=lambda target: list(target._https_proxies),
        apply_https_rows=apply_https,
        https_probe=https_probe,
        persist_vpn_config=lambda updates: persisted.append(updates),
    )
    return app, pool, applied_socks5, applied_https, persisted, probes


def test_socks5_read_add_remove_toggle():
    app, pool, applied, _, _, _ = _app()
    client = TestClient(app)
    payload = client.get("/api/vpn/socks5").json()
    assert payload["mode"] == "socks5"
    assert payload["proxies"][0]["label"] == "lab"
    add = client.post(
        "/api/vpn/socks5",
        json={"host": "3.3.3.3", "port": "1081", "username": "u"},
    ).json()
    assert add["ok"] is True
    assert applied[-1][1] == "socks5_add"
    assert add["proxies"][-1]["port"] == 1081
    removed = client.post("/api/vpn/socks5/remove", json={"index": 0}).json()
    assert removed["ok"] is True
    assert applied[-1][1] == "socks5_remove"
    toggled = client.post(
        "/api/vpn/socks5/toggle",
        json={"index": 0, "enabled": False},
    ).json()
    assert toggled == {"ok": True}
    assert pool._socks5_proxies[0]["enabled"] is False
    assert client.post(
        "/api/vpn/socks5/toggle",
        json={"index": 9},
    ).json()["error"].startswith("proxy index")


def test_socks5_egress_refresh_and_rotate():
    app, pool, *_ = _app()
    client = TestClient(app)
    payload = client.post("/api/vpn/socks5/refresh-egress").json()
    assert payload == {
        "ok": True,
        "results": {"ep": "ok"},
        "duplicates": {"dup": 1},
    }
    assert pool.refreshed == [True]
    rotated = client.post("/api/vpn/socks5/rotate-egress").json()
    assert rotated == {"ok": True, "started": 2, "scan": [{"station": 1}]}
    assert pool.rotated_managed == 1


def test_socks5_probe_uses_stored_row_and_updates_health():
    app, pool, _, _, _, probes = _app()
    payload = TestClient(app).post(
        "/api/vpn/socks5/test",
        json={"host": "ignored", "port": 1, "label": "lab"},
    ).json()
    assert payload["ok"] is True
    assert probes == [
        ("socks5", "1.1.1.1", 1080, {"username": None, "password": None, "ipv6": False})
    ]
    assert pool._station_health.repairs == 1
    assert pool._station_health.observed == [(3, True), (3, True)]


def test_socks5_rotate_toggle_and_manual():
    app, pool, _, _, persisted, _ = _app()
    client = TestClient(app)
    assert client.post(
        "/api/vpn/socks5/rotate",
        json={"rotate": True},
    ).json() == {"ok": True, "rotate": True}
    assert persisted == [{"socks5_auto_rotate": True}]
    assert pool._socks5_auto_rotate is True
    assert client.post("/api/vpn/socks5/rotate").json() == {
        "ok": True,
        "next": "proxy-1",
    }
    assert pool.rotated_now == 1


def test_https_add_remove_toggle_fallback_and_probe():
    app, pool, _, applied, persisted, probes = _app()
    client = TestClient(app)
    assert client.get("/api/vpn/https-proxies").json()["enabled"] is False
    add = client.post(
        "/api/vpn/https-proxies",
        json={"host": "4.4.4.4", "port": "90"},
    ).json()
    assert add["ok"] is True
    assert add["proxies"][-1]["port"] == 90
    assert client.post(
        "/api/vpn/https-proxies/toggle",
        json={"index": 0, "enabled": False},
    ).json() == {"ok": True}
    assert pool._https_proxies[0]["enabled"] is False
    assert client.post(
        "/api/vpn/https-proxies/remove",
        json={"index": 0},
    ).json()["ok"] is True
    assert applied[-1] == pool._https_proxies
    assert client.post(
        "/api/vpn/https-proxies/fallback",
        json={"enabled": True},
    ).json() == {"ok": True, "enabled": True}
    assert persisted == [{"https_fallback_enabled": True}]
    assert pool._https_fallback_enabled is True
    assert client.post(
        "/api/vpn/https-proxies/test",
        json={"host": "5.5.5.5", "port": 89},
    ).json() == {"ok": True}
    assert probes == [("https", "5.5.5.5", 89, {"username": "u", "password": "p"})]


def test_socks5_fallback_and_token():
    app, pool, _, _, persisted, _ = _app()
    assert TestClient(app).post(
        "/api/vpn/socks5/fallback",
        json={"enabled": True},
    ).json() == {"ok": True, "enabled": True}
    assert persisted == [{"socks5_fallback_enabled": True}]
    assert pool._socks5_fallback_enabled is True

    token_app, *_ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(token_app).post(
        "/api/vpn/socks5",
        json={"host": "x"},
    ).status_code == 401
