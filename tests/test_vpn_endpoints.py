"""test_vpn_endpoints.py — the 13 formerly-dead VPN dashboard endpoints
(plan 18/08 axe 3.1) through the real FastAPI routes.

All external I/O (NordVPN API, docker CLI, raw SOCKS5 probes, subprocess,
filesystem writes for upload-config) lives in isolated, monkeypatchable
helpers — this file never touches the live system. Same fixture pattern as
test_vpn_config_http.py / test_vpn_stack_persist.py.

Contract table (static/app.js consumers in parentheses):
GET  /api/vpn/socks5            {proxies, rotate}      — passwords masked
POST /api/vpn/socks5            {host, port, ...}      — append + persist
POST /api/vpn/socks5/remove     {index}                — bounds-checked
POST /api/vpn/socks5/toggle     {index, enabled}
POST /api/vpn/socks5/test       {host, port} → probe result
POST /api/vpn/socks5/rotate     {rotate: bool}=toggle | no body=manual next
POST /api/vpn/proxy-mode        {mode: vpn|socks5|direct} → fans out + persist
GET  /api/vpn/nordvpn-available {available}
GET  /api/vpn/nordvpn-status    {connected, country, city, ip} — honest adapter
GET  /api/vpn/nordvpn-countries {countries}
GET  /api/vpn/countries         {countries}            — same source
POST /api/vpn/discover-and-add  {country} → {count, country} + server_countries
POST /api/vpn/upload-config     FormData {name, config} → vpn_configs/custom/
GET  /api/vpn/diagnostic        bundle (mode, status, docker, wsl2, ...)
GET  /api/vpn-stack-info        {stations: {n: stack_info()}}  (shared)

Plus the axe 3.3 auth invariant (all POSTs guarded via _check_dashboard_token,
read-only GETs open) and the axe 3.4 config_yaml_dirty flag surfaced by
/api/vpn-status.
"""

import io
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import dashboard.api as api
import shared_state
from dashboard.api import register_dashboard


class _FakeMgr:
    """Station manager slice: everything the VPN endpoints touch."""

    def __init__(self, station, status="connected", ip="1.2.3.4"):
        self._station = station
        self.status = status  # plain attribute (diagnostic/nordvpn-status)
        self.current_ip = ip
        self.proxy_mode = "vpn"
        self._docker_compose_file = None
        self.config_updates = []

    def get_status(self):
        return {
            "status": self.status,
            "current_country": "France",
            "next_country": "Germany",
            "country_pinned_at": None,
            "country_rotation": True,
            "ip": self.current_ip,
            "city": "Paris",
        }

    async def refresh_status(self):
        return None

    def get_config(self):
        return {"enabled": True, "vpn_stack": "auto", "station_count": 2}

    def stack_info(self):
        return {"station": self._station, "status": self.status, "stack": "wireguard"}

    async def update_config(self, updates: dict) -> dict:
        self.config_updates.append(dict(updates))
        return {}


class _NoCloseBytesIO(io.BytesIO):
    """BytesIO whose close() is a no-op — the endpoint's ``with open(...)``
    context manager closes the handle; the test still needs getvalue()."""

    def close(self):
        pass


class _FakePool:
    """Free-IP pool slice: socks5 list + auto-rotate + rotate_socks5_now."""

    def __init__(self, rows=None):
        self._socks5_proxies = list(rows or [])
        self._socks5_auto_rotate = True
        self._socks5_fallback_enabled = False
        self._https_proxies = []
        self._https_fallback_enabled = False
        self.set_calls = []
        self.https_set_calls = []

    def set_socks5_proxies(self, rows):
        self._socks5_proxies = [dict(r) for r in rows]
        self.set_calls.append([dict(r) for r in rows])

    def set_https_proxies(self, rows):
        self._https_proxies = [dict(r) for r in rows]
        self.https_set_calls.append([dict(r) for r in rows])

    def rotate_socks5_now(self):
        return SimpleNamespace(pid="nxt.example.com")

    def get_status(self):
        return {"enabled": True, "status": "connected", "current_country": "France"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """FastAPI app + fakes, never the live system. ``cfg`` is a plain dict
    patched into config_settings.IP_ROTATION (without the socks5 keys, so
    _socks5_auto_rotate_state falls back to the pool) — tests mutate it."""
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    register_dashboard(fast, str(tmp_path), sqlite3.connect(":memory:"))

    s1 = _FakeMgr(1)
    s2 = _FakeMgr(2)
    pool = _FakePool(
        rows=[
            {
                "host": "p1.example.com",
                "port": 1080,
                "enabled": True,
                "username": "u1",
                "password": "s3cr3t",
            },
            {
                "host": "p2.example.com",
                "port": 1081,
                "enabled": False,
                "username": None,
                "password": None,
            },
        ]
    )
    monkeypatch.setattr(shared_state, "vpn_managers", [s1, s2], raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager", s1, raising=False)
    # Deterministic config mirror (never the real config.yaml) + no env
    # divergence noise from vpn-status.
    cfg = {"server_provider": "nordvpn", "server_countries": "Netherlands"}
    monkeypatch.setattr(api.config_settings, "IP_ROTATION", cfg, raising=False)
    monkeypatch.setattr(api.config_settings, "ENV_DIVERGENCE", [], raising=False)
    # Dashboard auth OFF by default (each auth test sets os.environ-free token).
    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "", raising=False)
    # Persist recorder — never touches config.yaml.
    persisted = []
    monkeypatch.setattr(api, "_persist_vpn_config", lambda u: (persisted.append(dict(u)), None)[1])
    # Audit recorder — no test should write the real logs/egress_audit.jsonl.
    monkeypatch.setattr(api, "_egress_audit", lambda *a, **k: None)
    return fast, s1, s2, pool, cfg, persisted


def _post(app, path, body=None):
    with TestClient(app) as client:
        return client.post(path, json=body or {}).json()


# ── GET /api/vpn/socks5 — passwords never leak ────────────────────


def test_socks5_get_masks_passwords(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    with TestClient(fast) as client:
        resp = client.get("/api/vpn/socks5").json()
    assert resp["rotate"] is True  # pool toggle (cfg has no key)
    proxies = {p["host"]: p for p in resp["proxies"]}
    assert set(proxies) == {"p1.example.com", "p2.example.com"}
    for p in resp["proxies"]:
        assert "password" not in p, "password must never reach the browser"
        assert "username" not in p
    assert proxies["p1.example.com"]["enabled"] is True
    assert proxies["p1.example.com"]["has_password"] is True
    assert proxies["p2.example.com"]["enabled"] is False
    assert proxies["p2.example.com"]["has_password"] is False


# ── Auth (axe 3.2): POSTs guarded, read-only GETs open ────────────


def test_posts_require_token_when_configured(ctx, monkeypatch):
    """[v9 §14.0.3] Zéro friction : depuis le réseau de confiance (loopback /
    TestClient), les POST passent SANS token — le réseau est l'identifiant.
    La contrainte 401 sans token ne s'applique plus qu'aux réseaux non
    approuvés (couvert par tests/test_trust.py côté middleware)."""
    fast, s1, s2, pool, cfg, persisted = ctx
    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "sekret")
    with TestClient(fast) as client:
        # No header → 200 (loopback de confiance, v9 zéro friction).
        r = client.post("/api/vpn/proxy-mode", json={"mode": "socks5"})
        assert r.status_code == 200
        assert s1.proxy_mode == "socks5"
        # Read-only GET stays open (the dashboard itself polls it).
        assert client.get("/api/vpn/socks5").status_code == 200


# ── POST /api/vpn/proxy-mode ──────────────────────────────────────


def test_proxy_mode_sets_all_managers_and_persists(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/proxy-mode", {"mode": "socks5"})
    assert resp == {"ok": True, "mode": "socks5"}
    assert s1.proxy_mode == "socks5"
    assert s2.proxy_mode == "socks5"
    assert persisted == [
        {
            "proxy_mode": "socks5",
            "egress_providers": ["warp"],
            "egress_strategy": "fallback",
            "egress_weights": {},
        }
    ]


def test_proxy_mode_providers_derives_auto_egress(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/vpn/proxy-mode",
        {"providers": ["warp", "nordvpn"]},
    )
    assert resp == {"ok": True, "mode": "auto_egress"}
    # Runtime = vpn (NordVPN primary, WARP fallback) ; l'ordre est persisté.
    assert s1.proxy_mode == "vpn"
    assert s2.proxy_mode == "vpn"
    assert persisted[0] == {
        "proxy_mode": "vpn",
        "egress_providers": ["warp", "nordvpn"],
        "egress_strategy": "fallback",
        "egress_weights": {},
    }


def test_proxy_mode_balance_persists_normalized_weights(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/vpn/proxy-mode",
        {
            "providers": ["nordvpn", "warp", "proxies"],
            "strategy": "balance",
            "weights": {"nordvpn": 2, "warp": 1, "proxies": 1},
        },
    )
    assert resp == {"ok": True, "mode": "auto_egress"}
    assert persisted[0]["egress_strategy"] == "balance"
    assert persisted[0]["egress_weights"] == {
        "nordvpn": 50,
        "warp": 25,
        "proxies": 25,
    }


def test_proxy_mode_balance_without_weights_equalizes(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/vpn/proxy-mode",
        {"providers": ["warp", "proxies"], "strategy": "balance"},
    )
    assert resp == {"ok": True, "mode": "socks5"}
    assert persisted[0]["egress_weights"] == {"warp": 50, "proxies": 50}


def test_proxy_mode_fallback_keeps_weights_but_ignores_them(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/vpn/proxy-mode",
        {
            "providers": ["warp", "nordvpn"],
            "strategy": "fallback",
            "weights": {"warp": 10, "nordvpn": 90},
        },
    )
    assert resp == {"ok": True, "mode": "auto_egress"}
    assert persisted[0]["egress_strategy"] == "fallback"
    assert persisted[0]["egress_weights"] == {"warp": 10, "nordvpn": 90}


def test_proxy_mode_balance_four_providers_including_direct(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    providers = ["nordvpn", "warp", "proxies", "direct"]
    resp = _post(
        fast,
        "/api/vpn/proxy-mode",
        {"providers": providers, "strategy": "balance"},
    )
    assert resp == {"ok": True, "mode": "auto_egress"}
    assert persisted[0]["egress_providers"] == providers
    assert persisted[0]["egress_weights"] == {
        "nordvpn": 25,
        "warp": 25,
        "proxies": 25,
        "direct": 25,
    }


def test_egress_capacity_endpoint_full_6_6(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    cfg.update(
        {
            "station_count": 6,
            "egress_providers": ["nordvpn", "warp"],
            "egress_strategy": "fallback",
            "egress_weights": {},
            "egress_capacity": {"target_slots": 6, "reserve_policy": "full"},
        }
    )
    monkeypatch.setattr(
        api,
        "_egress_capacity_counts",
        lambda: {
            "nordvpn": {"configured": 6, "enabled": 6, "usable": 6},
            "warp": {"configured": 6, "enabled": 6, "usable": 6},
            "proxies": {"configured": 0, "enabled": 0, "usable": 0},
            "direct": {"configured": 1, "enabled": 1, "usable": 1},
        },
    )
    with TestClient(fast) as client:
        data = client.get("/api/egress/capacity").json()
    assert data["target_slots"] == 6
    assert data["reserve_policy"] == "full"
    assert data["verdict"] == "ok"
    assert data["total_usable"] == 12
    assert data["total_required"] == 12
    assert data["providers"]["nordvpn"]["required"] == 6
    assert data["providers"]["warp"]["required"] == 6


def test_egress_capacity_endpoint_shared_3_3_warns(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    cfg.update(
        {
            "egress_providers": ["nordvpn", "warp"],
            "egress_strategy": "balance",
            "egress_weights": {"nordvpn": 50, "warp": 50},
            "egress_capacity": {"target_slots": 6, "reserve_policy": "shared"},
        }
    )
    monkeypatch.setattr(
        api,
        "_egress_capacity_counts",
        lambda: {
            "nordvpn": {"configured": 3, "enabled": 3, "usable": 3},
            "warp": {"configured": 3, "enabled": 3, "usable": 3},
            "proxies": {"configured": 0, "enabled": 0, "usable": 0},
            "direct": {"configured": 1, "enabled": 1, "usable": 1},
        },
    )
    with TestClient(fast) as client:
        data = client.get("/api/egress/capacity").json()
    assert data["verdict"] == "ok"
    assert data["total_usable"] == 6
    assert any(w["code"] == "no_reserve" for w in data["warnings"])


def test_egress_capacity_post_persists_target_and_policy(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/egress/capacity",
        {"target_slots": 8, "reserve_policy": "shared"},
    )
    assert resp == {
        "ok": True,
        "egress_capacity": {"target_slots": 8, "reserve_policy": "shared"},
    }
    assert persisted[-1] == {
        "egress_capacity": {"target_slots": 8, "reserve_policy": "shared"}
    }


def test_egress_capacity_post_validates_bounds(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    assert "entre 1 et 10" in _post(
        fast, "/api/egress/capacity", {"target_slots": 0}
    )["error"]
    assert "full|shared|proportional" in _post(
        fast, "/api/egress/capacity", {"reserve_policy": "nope"}
    )["error"]
    assert "requis" in _post(fast, "/api/egress/capacity", {})["error"]
    assert persisted == []


def test_egress_capacity_includes_observed_share(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    cfg.update(
        {
            "egress_providers": ["nordvpn", "warp"],
            "egress_strategy": "balance",
            "egress_weights": {"nordvpn": 50, "warp": 50},
            "egress_capacity": {"target_slots": 6, "reserve_policy": "full"},
        }
    )
    monkeypatch.setattr(
        api,
        "_egress_capacity_counts",
        lambda: {
            "nordvpn": {"configured": 6, "enabled": 6, "usable": 6},
            "warp": {"configured": 6, "enabled": 6, "usable": 6},
            "proxies": {"configured": 0, "enabled": 0, "usable": 0},
            "direct": {"configured": 1, "enabled": 1, "usable": 1},
        },
    )
    monkeypatch.setattr(
        api,
        "_egress_observed_counts",
        lambda: {"nordvpn": 90, "warp": 10},
    )
    with TestClient(fast) as client:
        data = client.get("/api/egress/capacity").json()
    assert data["observed_requests"] == 100
    assert data["providers"]["nordvpn"]["observed_share"] == 90.0
    assert data["providers"]["warp"]["observed_share"] == 10.0
    assert any(w["code"] == "observed_shift" for w in data["warnings"])


def test_egress_capacity_post_writes_audit(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    records: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        api,
        "_egress_audit",
        lambda action, details: records.append((action, dict(details))),
    )
    resp = _post(
        fast,
        "/api/egress/capacity",
        {"target_slots": 7, "reserve_policy": "shared"},
    )
    assert resp["ok"] is True
    assert records == [
        (
            "capacity_config",
            {"egress_capacity": {"target_slots": 7, "reserve_policy": "shared"}},
        )
    ]


def test_proxy_mode_invalid_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/proxy-mode", {"mode": "banana"})
    assert "mode doit être vpn|socks5|direct" in resp["error"]
    assert persisted == []
    assert s1.proxy_mode == "vpn"


# ── POST /api/vpn/socks5 (append) ─────────────────────────────────


def test_socks5_add_persists_cleaned_row(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/vpn/socks5",
        {"host": "p3.example.com", "port": 1090, "username": "u3", "password": "pw3"},
    )
    assert resp["ok"] is True
    assert persisted == [
        {
            "socks5_proxies": [
                {
                    "host": "p1.example.com",
                    "port": 1080,
                    "enabled": True,
                    "username": "u1",
                    "password": "s3cr3t",
                    "label": "",
                    "ipv6": False,
                    "egress_ip": "",
                    "managed": False,
                    "container": "",
                    "image": "",
                    "block": "",
                    "provider": "",
                },
                {
                    "host": "p2.example.com",
                    "port": 1081,
                    "enabled": False,
                    "username": None,
                    "password": None,
                    "label": "",
                    "ipv6": False,
                    "egress_ip": "",
                    "managed": False,
                    "container": "",
                    "image": "",
                    "block": "",
                    "provider": "",
                },
                {
                    "host": "p3.example.com",
                    "port": 1090,
                    "enabled": True,
                    "username": "u3",
                    "password": "pw3",
                    "label": "",
                    "ipv6": False,
                    "egress_ip": "",
                    "managed": False,
                    "container": "",
                    "image": "",
                    "block": "",
                    "provider": "",
                },
            ]
        }
    ]
    assert len(pool._socks5_proxies) == 3
    # The response payload re-masks the password.
    for p in resp["proxies"]:
        assert "password" not in p
    assert resp["proxies"][2]["host"] == "p3.example.com"
    assert resp["proxies"][2]["has_password"] is True


def test_socks5_toggle_preserves_warp_metadata(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    pool._socks5_proxies.append(
        {
            "host": "127.0.0.1",
            "port": 11080,
            "enabled": True,
            "label": "warp-ipv6",
            "managed": True,
            "provider": "warp",
            "ipv6": True,
            "egress_ip": "2a09:bac5:32d0:14dc::214:83",
            "container": "warp-test",
            "image": "caomingjun/warp:latest",
            "block": "2a09:bac5:32d0:14dc::/64",
        }
    )
    resp = _post(fast, "/api/vpn/socks5/toggle", {"index": 2, "enabled": False})
    assert resp["ok"] is True
    row = persisted[0]["socks5_proxies"][2]
    assert row["enabled"] is False
    assert row["managed"] is True
    assert row["provider"] == "warp"
    assert row["label"] == "warp-ipv6"


def test_socks5_add_invalid_port_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5", {"host": "x", "port": "abc"})
    assert "port invalide" in resp["error"]
    assert persisted == []
    assert len(pool._socks5_proxies) == 2, "no row appended on invalid port"


def test_socks5_add_port_out_of_range_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5", {"host": "x", "port": 70000})
    assert "port hors bornes" in resp["error"]
    assert persisted == [], "validation failure never persists"
    assert len(pool._socks5_proxies) == 2


# ── POST /api/vpn/socks5/remove + toggle ──────────────────────────


def test_socks5_remove_by_index(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5/remove", {"index": 1})
    assert resp["ok"] is True
    assert [p["host"] for p in resp["proxies"]] == ["p1.example.com"]
    assert [r["host"] for r in pool._socks5_proxies] == ["p1.example.com"]


def test_socks5_remove_out_of_bounds_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5/remove", {"index": 99})
    assert "proxy index hors bornes" in resp["error"]
    assert persisted == []
    assert len(pool._socks5_proxies) == 2


def test_socks5_toggle(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5/toggle", {"index": 1, "enabled": True})
    assert resp == {"ok": True}
    assert pool._socks5_proxies[1]["enabled"] is True
    assert persisted[-1]["socks5_proxies"][1]["enabled"] is True


# ── POST /api/vpn/socks5/rotate ───────────────────────────────────


def test_socks5_rotate_toggle_persists(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5/rotate", {"rotate": False})
    assert resp == {"ok": True, "rotate": False}
    assert persisted == [{"socks5_auto_rotate": False}]
    assert pool._socks5_auto_rotate is False


def test_socks5_rotate_manual_returns_next(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/socks5/rotate", {})
    assert resp == {"ok": True, "next": "nxt.example.com"}
    assert persisted == [], "manual rotate never persists"


def test_persist_vpn_config_writes_proxy_fallback_keys(tmp_path, monkeypatch):
    """The dedicated toggle endpoints call the real writer; the key_map must
    include the proxy keys or the GUI silently forgets them after restart."""
    import copy

    import yaml

    import config.settings as cs

    before = copy.deepcopy(cs._yaml_data.get("ip_rotation", {}))
    target = tmp_path / "config.yaml"
    target.write_text("ip_rotation:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(api, "__CONFIG_YAML_PATH", str(target))
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 0.0)
    try:
        err = api._persist_vpn_config(
            {
                "socks5_fallback_enabled": True,
                "https_fallback_enabled": True,
                "socks5_proxies": [{"host": "socks.example.com", "port": 1080}],
                "https_proxies": [{"host": "https.example.com", "port": 89}],
            }
        )
        assert err is None
        on_disk = yaml.safe_load(target.read_text(encoding="utf-8"))["ip_rotation"]
        assert on_disk["socks5_fallback_enabled"] is True
        assert on_disk["https_fallback_enabled"] is True
        assert on_disk["socks5_proxies"][0]["port"] == 1080
        assert on_disk["https_proxies"][0]["port"] == 89
    finally:
        # The writer intentionally syncs config.settings._yaml_data in place;
        # restore it or a later save_yaml_config() would leak these fixtures
        # into the real config.yaml.
        cs._yaml_data["ip_rotation"].clear()
        cs._yaml_data["ip_rotation"].update(before)


def test_socks5_fallback_toggle_persists_and_updates_pool(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    with TestClient(fast) as client:
        assert client.get("/api/vpn/socks5").json()["fallback"] is False
    resp = _post(fast, "/api/vpn/socks5/fallback", {"enabled": True})
    assert resp == {"ok": True, "enabled": True}
    assert persisted == [{"socks5_fallback_enabled": True}]
    assert pool._socks5_fallback_enabled is True
    with TestClient(fast) as client:
        assert client.get("/api/vpn/socks5").json()["fallback"] is True


# ── POST /api/vpn/socks5/test (probe) ─────────────────────────────


def test_socks5_test_delegates_to_probe_with_stored_credentials(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    calls = []

    async def _fake_probe(host, port, username=None, password=None, *, ipv6=False):
        calls.append((host, port, username, password))
        return {"ok": True, "ip": "5.5.5.5", "opencode_ok": True, "latency_ms": 12.3}

    monkeypatch.setattr(api, "_socks5_probe", _fake_probe)
    resp = _post(fast, "/api/vpn/socks5/test", {"host": "p1.example.com", "port": 1080})
    assert resp == {"ok": True, "ip": "5.5.5.5", "opencode_ok": True, "latency_ms": 12.3}
    # NordVPN SOCKS5 requires auth: the stored row supplies it server-side.
    assert calls == [("p1.example.com", 1080, "u1", "s3cr3t")]


def test_socks5_test_label_selects_ipv6_row(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    pool._socks5_proxies.append(
        {
            "host": "127.0.0.1",
            "port": 11080,
            "label": "warp-ipv6",
            "ipv6": True,
            "enabled": True,
            "username": None,
            "password": None,
        }
    )
    calls = []

    async def _fake_probe(host, port, username=None, password=None, *, ipv6=False):
        calls.append((host, port, ipv6))
        return {"ok": True}

    monkeypatch.setattr(api, "_socks5_probe", _fake_probe)
    resp = _post(
        fast,
        "/api/vpn/socks5/test",
        {"host": "ignored", "port": 1, "label": "warp-ipv6"},
    )
    assert resp == {"ok": True}
    assert calls == [("127.0.0.1", 11080, True)]


# ?? HTTPS CONNECT proxies (NordVPN :89) ????????????????????????????


def test_https_get_empty(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    with TestClient(fast) as client:
        resp = client.get("/api/vpn/https-proxies").json()
    assert resp == {"enabled": False, "proxies": []}


def test_https_add_persists_and_masks_password(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(
        fast,
        "/api/vpn/https-proxies",
        {"host": "uk765.nordvpn.com", "username": "u", "password": "pw"},
    )
    assert resp["ok"] is True
    assert resp["proxies"] == [
        {"host": "uk765.nordvpn.com", "port": 89, "enabled": True, "has_password": True}
    ]
    assert "password" not in resp["proxies"][0]
    assert persisted[-1]["https_proxies"][0]["port"] == 89
    assert pool._https_proxies[0]["password"] == "pw"
    assert pool.https_set_calls


def test_https_add_invalid_port_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/https-proxies", {"host": "x", "port": 70000})
    assert "port hors bornes" in resp["error"]
    assert persisted == []
    assert pool._https_proxies == []


def test_https_remove_and_toggle_bounds(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    pool._https_proxies = [
        {"host": "a.example.com", "port": 89, "enabled": True, "username": None, "password": None}
    ]
    resp = _post(fast, "/api/vpn/https-proxies/toggle", {"index": 0, "enabled": False})
    assert resp == {"ok": True}
    assert pool._https_proxies[0]["enabled"] is False
    resp = _post(fast, "/api/vpn/https-proxies/remove", {"index": 0})
    assert resp["ok"] is True and resp["proxies"] == []
    resp = _post(fast, "/api/vpn/https-proxies/remove", {"index": 99})
    assert "hors bornes" in resp["error"]


def test_https_fallback_toggle_persists_and_updates_pool(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/https-proxies/fallback", {"enabled": True})
    assert resp == {"ok": True, "enabled": True}
    assert persisted == [{"https_fallback_enabled": True}]
    assert pool._https_fallback_enabled is True
    with TestClient(fast) as client:
        assert client.get("/api/vpn/https-proxies").json()["enabled"] is True


def test_https_test_delegates_to_probe_with_stored_credentials(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    pool._https_proxies = [
        {
            "host": "uk765.nordvpn.com",
            "port": 89,
            "enabled": True,
            "username": "u",
            "password": "pw",
        }
    ]
    calls = []

    async def _fake_probe(host, port, username=None, password=None):
        calls.append((host, port, username, password))
        return {"ok": True, "ip": "6.6.6.6", "opencode_ok": True, "latency_ms": 9.0}

    monkeypatch.setattr(api, "_https_probe", _fake_probe)
    resp = _post(fast, "/api/vpn/https-proxies/test", {"host": "uk765.nordvpn.com", "port": 89})
    assert resp["ip"] == "6.6.6.6"
    assert calls == [("uk765.nordvpn.com", 89, "u", "pw")]


# ── GET nordvpn-available / nordvpn-status ────────────────────────


def test_nordvpn_available_depends_on_creds_and_provider(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx

    def _exists(p):
        return p.replace("\\", "/").endswith("/credentials.env")

    monkeypatch.setattr(api.os.path, "exists", _exists)
    monkeypatch.setattr(api.os.path, "getsize", lambda p: 1024)

    cfg["server_provider"] = "nordvpn"
    with TestClient(fast) as client:
        assert client.get("/api/vpn/nordvpn-available").json() == {"available": True}
    cfg["server_provider"] = "openvpn"
    with TestClient(fast) as client:
        assert client.get("/api/vpn/nordvpn-available").json() == {"available": False}
    # Credentials missing → False regardless of provider.
    monkeypatch.setattr(api.os.path, "exists", lambda p: False)
    cfg["server_provider"] = "nordvpn"
    with TestClient(fast) as client:
        assert client.get("/api/vpn/nordvpn-available").json() == {"available": False}


def test_nordvpn_status_is_honest_adapter(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    s1.status = "connected"
    s1.current_ip = "9.9.9.9"
    with TestClient(fast) as client:
        st = client.get("/api/vpn/nordvpn-status").json()
    assert st["connected"] is True
    assert st["country"] == "France"
    assert st["ip"] == "9.9.9.9"
    assert st["city"] == "Paris"


# ── GET nordvpn-countries / countries ─────────────────────────────


def test_countries_static_when_api_off(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    small = [{"code": "DE", "name": "Germany"}, {"code": "FR", "name": "France"}]
    monkeypatch.setattr(api, "_NORDVPN_STATIC_COUNTRIES", small)
    cfg["use_nordvpn_api"] = False
    with TestClient(fast) as client:
        assert client.get("/api/vpn/nordvpn-countries").json() == {"countries": small}
        assert client.get("/api/vpn/countries").json() == {"countries": small}


def test_countries_via_api_when_enabled(ctx, monkeypatch):
    """use_nordvpn_api=on → _nordvpn_api_fetch parsed into {code, name}
    uppercased/normalized + sorted by name."""
    fast, s1, s2, pool, cfg, persisted = ctx

    async def _fake_fetch(url, timeout=10.0):
        return [{"code": "fr", "name": " France "}, {"code": "de", "name": "Germany"}]

    monkeypatch.setattr(api, "_nordvpn_api_fetch", _fake_fetch)
    cfg["use_nordvpn_api"] = True
    with TestClient(fast) as client:
        got = client.get("/api/vpn/countries").json()["countries"]
    # sorted by name: France < Germany.
    assert got == [{"code": "FR", "name": "France"}, {"code": "DE", "name": "Germany"}]


# ── POST /api/vpn/discover-and-add ────────────────────────────────


def test_discover_and_add_merges_country(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    seen = {}

    async def _fake_servers(code, limit=20):
        seen["code"] = code
        seen["limit"] = limit
        return [{"hostname": f"{code.lower()}1.nordvpn.com", "country": code, "load": 12}]

    async def _fake_countries(use_api):
        return [{"code": "DE", "name": "Germany"}]

    monkeypatch.setattr(api, "_nordvpn_servers_by_country", _fake_servers)
    monkeypatch.setattr(api, "_nordvpn_countries", _fake_countries)
    cfg["server_countries"] = "Netherlands, France"

    resp = _post(fast, "/api/vpn/discover-and-add", {"country": "de", "limit": 999})
    assert resp == {"count": 1, "country": "Germany"}
    assert seen["code"] == "DE"
    assert seen["limit"] == 50, "limit clamped to 50"
    assert persisted == [{"server_countries": "Netherlands, France, Germany"}]


def test_discover_and_add_no_servers_is_error(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx

    async def _fake_servers(code, limit=20):
        return []

    async def _fake_countries(use_api):
        return []

    monkeypatch.setattr(api, "_nordvpn_servers_by_country", _fake_servers)
    monkeypatch.setattr(api, "_nordvpn_countries", _fake_countries)
    resp = _post(fast, "/api/vpn/discover-and-add", {"country": "XX"})
    assert "aucun serveur trouvé" in resp["error"]
    assert persisted == []


def test_discover_and_add_dedups_existing_country(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx

    async def _fake_servers(code, limit=20):
        return [{"hostname": "de1.nordvpn.com", "country": "DE", "load": 5}]

    async def _fake_countries(use_api):
        return [{"code": "DE", "name": "Germany"}]

    monkeypatch.setattr(api, "_nordvpn_servers_by_country", _fake_servers)
    monkeypatch.setattr(api, "_nordvpn_countries", _fake_countries)
    cfg["server_countries"] = "Germany"
    resp = _post(fast, "/api/vpn/discover-and-add", {"country": "DE"})
    assert resp["country"] == "Germany"
    assert persisted == [{"server_countries": "Germany"}], "no duplicate name"


def test_discover_and_add_missing_code_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    resp = _post(fast, "/api/vpn/discover-and-add", {})
    assert "country requis" in resp["error"]
    assert persisted == []


# ── POST /api/vpn/upload-config (FormData, filesystem intercepted) ─


class _UploadSink:
    """Capture the bytes written by the endpoint's ``with open(path) as f``.

    The endpoint closes the handle on exit — a real BytesIO would be closed
    before the assertion could call getvalue() — so capture at write-time.
    """

    def __init__(self, path):
        self.path = path
        self.data = bytearray()

    def write(self, b):
        self.data += b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_upload_config_writes_custom_dir_and_persists(ctx, monkeypatch, tmp_path):
    fast, s1, s2, pool, cfg, persisted = ctx
    makedirs_calls = []
    monkeypatch.setattr(api.os, "makedirs", lambda *a, **k: makedirs_calls.append((a, k)))
    opened = []
    real_open = io.open

    def _fake_open(path, mode="r", *args, **kw):
        if "vpn_configs" in str(path):
            buf = _UploadSink(str(path))
            opened.append(buf)
            return buf
        return real_open(path, mode, *args, **kw)

    # upload-config uses the builtin ``open`` (module-global lookup) → patch
    # builtins (delegating wrapper, so starlette internals still work).
    monkeypatch.setattr(__import__("builtins"), "open", _fake_open)

    body = b"client\nremote fr1.nordvpn.com 1194 udp\n"
    with TestClient(fast) as client:
        resp = client.post(
            "/api/vpn/upload-config",
            data={"name": "nordvigre"},
            files={"config": ("nordvigre.ovpn", body, "text/plain")},
        ).json()

    assert resp == {"ok": True, "path": "vpn_configs/custom/nordvigre.ovpn"}
    assert persisted == [{"custom_ovpn_file": "vpn_configs/custom/nordvigre.ovpn"}]
    assert opened and opened[0].path.replace("\\", "/").endswith("vpn_configs/custom/nordvigre.ovpn")
    assert bytes(opened[0].data) == body
    assert makedirs_calls, "custom dir ensured before write"


def test_upload_config_sanitizes_name(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    monkeypatch.setattr(api.os, "makedirs", lambda *a, **k: None)
    opened = []

    def _fake_open(path, mode="r", *a, **k):
        if "vpn_configs" in str(path):
            opened.append(str(path))
            return io.BytesIO()
        return open(path, mode, *a, **k)

    monkeypatch.setattr(__import__("builtins"), "open", _fake_open)
    with TestClient(fast) as client:
        resp = client.post(
            "/api/vpn/upload-config",
            data={"name": "my conf!/"},
            files={"config": ("x", b"client\n", "text/plain")},
        ).json()
    assert resp["ok"] is True
    assert resp["path"].endswith("my_conf__.ovpn")
    assert persisted[-1]["custom_ovpn_file"] == resp["path"]


def test_upload_config_missing_file_is_error(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    with TestClient(fast) as client:
        resp = client.post(
            "/api/vpn/upload-config",
            data={"name": "x.ovpn"},
            files={"config": ("x.ovpn", b"", "text/plain")},
        ).json()
    assert "fichier config manquant" in resp["error"]
    assert persisted == []


# ── GET /api/vpn/diagnostic (docker/wsl/subprocess all stubbed) ───


def test_diagnostic_bundle(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    s1.status = "connected"
    s1.current_ip = "7.7.7.7"
    monkeypatch.setattr(api, "_docker_diag", lambda: {"available": True, "running": True, "version": "v1.2"})
    monkeypatch.setattr(api, "_docker_compose_config", lambda: ["=== compose: OK"])
    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(api.os.path, "exists", lambda p: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(api, "_config_yaml_mtime", lambda: 123.0)
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 0.0)

    with TestClient(fast) as client:
        d = client.get("/api/vpn/diagnostic").json()

    assert d["current_mode"] == "vpn"
    assert d["status"] == "connected"
    assert d["ip"] == "7.7.7.7"
    assert d["wsl2"] == {"available": True}
    assert d["openvpn"] == {"available": False, "path": None}
    assert d["docker"]["version"] == "v1.2"
    assert d["config_yaml_dirty"] is True
    assert d["compose_config"] == ["=== compose: OK"]
    assert "Docker OK" in d["recommendation"] or "Connexion VPN active" in d["recommendation"]


# ── GET /api/vpn-stack-info (shared) ──────────────────────────────


def test_vpn_stack_info_per_station(ctx):
    fast, s1, s2, pool, cfg, persisted = ctx
    with TestClient(fast) as client:
        info = client.get("/api/vpn-stack-info").json()
    assert set(info["stations"]) == {"1", "2"}
    assert info["stations"]["1"]["stack"] == "wireguard"
    assert info["stations"]["1"]["station"] == 1


# ── axe 3.4: config_yaml_dirty surfaced by /api/vpn-status ────────


def test_vpn_status_exposes_config_yaml_dirty(ctx, monkeypatch):
    fast, s1, s2, pool, cfg, persisted = ctx
    monkeypatch.setattr(api, "_config_yaml_mtime", lambda: 55.0)
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 0.0)
    with TestClient(fast) as client:
        dirty = client.get("/api/vpn-status").json()["config_yaml_dirty"]
    assert dirty is True
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 55.0)
    with TestClient(fast) as client:
        clean = client.get("/api/vpn-status").json()["config_yaml_dirty"]
    assert clean is False
