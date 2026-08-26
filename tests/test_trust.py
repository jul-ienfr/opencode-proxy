"""[plan v10 §14.0.3-4] Confiance réseau zéro-friction (v9) : unit + ASGI e2e.

Couvre : ip_trusted (loopback/LAN/modes), Origin anti-CSRF, rate-limit
mutations, DashboardTrustMiddleware (/api/*), ClientAuthMiddleware
(/v1/* + resets), et le short-circuit zéro-friction de
_check_dashboard_token.
"""

import pytest

from trust import (
    ClientAuthMiddleware,
    DashboardTrustMiddleware,
    _MutationRateLimiter,
    ip_trusted,
    is_loopback,
    origin_host_matches,
    parse_cidrs,
    send_json,
)

# ── helpers ──────────────────────────────────────────────────────────────


def set_cfg(monkeypatch, section, value):
    import config.settings as st

    data = {k: v for k, v in st._yaml_data.items() if k != section}
    if value is not None:
        data[section] = value
    monkeypatch.setattr(st, "_yaml_data", data)


def make_scope(path="/api/stats", method="GET", ip="127.0.0.1", headers=None):
    hs = [(b"host", b"localhost:4000")]
    for k, v in (headers or {}).items():
        hs.append((k.lower().encode(), v.encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "client": (ip, 12345),
        "headers": hs,
    }


class SendCollector:
    def __init__(self):
        self.messages = []

    async def __call__(self, msg):
        self.messages.append(msg)

    @property
    def status(self):
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None


async def run_through(mw, scope):
    """Instancie un middleware avec app=None puis injecte l'inner app."""
    sent = SendCollector()

    async def inner(scope, receive, send):
        await send_json(send, 200, {"ok": "1"})

    mw.app = inner
    await mw(scope, None, sent)
    return sent.status


# ── unit : décision réseau ───────────────────────────────────────────────


def test_loopback_always_trusted(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "off"})
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert ip_trusted("127.0.0.1")


def test_lan_mode_trusts_private_ranges(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    assert ip_trusted("192.168.1.50")
    assert ip_trusted("10.20.30.40")
    assert ip_trusted("172.16.5.5")
    assert not ip_trusted("8.8.8.8")
    assert not ip_trusted("")
    assert not ip_trusted("pas-une-ip")


def test_off_mode_blocks_lan(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "off"})
    assert not ip_trusted("192.168.1.50")


def test_open_mode_trusts_everything(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "open"})
    assert ip_trusted("8.8.8.8")


def test_invalid_cidr_ignored():
    nets = parse_cidrs(["192.168.0.0/16", "n'importe-quoi"])
    assert len(nets) == 1


def test_custom_cidrs_hot_reloadable(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan", "lan_cidrs": ["100.64.0.0/10"]})
    assert ip_trusted("100.64.0.9")
    assert not ip_trusted("192.168.1.50"), "défauts remplacés par la config"


# ── unit : Origin anti-CSRF ──────────────────────────────────────────────


def _scope_with_headers(headers):
    return make_scope(headers=headers)


def test_origin_same_host_ok():
    assert origin_host_matches(_scope_with_headers({"Origin": "http://localhost:4000"}))


def test_origin_cross_site_blocked():
    assert not origin_host_matches(
        _scope_with_headers({"Origin": "https://evil.example", "Referer": ""})
    )


def test_referer_cross_site_blocked():
    assert not origin_host_matches(_scope_with_headers({"Referer": "https://evil.example/x"}))


def test_no_origin_no_referer_ok():
    assert origin_host_matches(_scope_with_headers({}))


# ── ASGI : DashboardTrustMiddleware ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dash_loopback_get_passes(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "")
    assert await run_through(mw, make_scope()) == 200


@pytest.mark.asyncio
async def test_dash_lan_get_passes_without_credentials(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "")
    assert await run_through(mw, make_scope(ip="192.168.1.50")) == 200


@pytest.mark.asyncio
async def test_dash_external_blocked_403(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "")
    assert await run_through(mw, make_scope(ip="203.0.113.9")) == 403


@pytest.mark.asyncio
async def test_dash_external_with_valid_token_passes(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "tok123")
    assert (
        await run_through(
            mw, make_scope(ip="203.0.113.9", headers={"X-Dashboard-Token": "tok123"})
        )
        == 200
    )


@pytest.mark.asyncio
async def test_dash_external_token_configured_missing_or_wrong_is_401(monkeypatch):
    """[v10] Sémantique historique préservée hors réseau de confiance : token
    configuré → 401 sans/avec mauvais token ; 403 réservé à l'absence de token."""
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "tok123")
    assert await run_through(mw, make_scope(ip="203.0.113.9")) == 401
    assert (
        await run_through(
            mw, make_scope(ip="203.0.113.9", headers={"X-Dashboard-Token": "nope"})
        )
        == 401
    )


@pytest.mark.asyncio
async def test_dash_require_token_forces_401_on_lan(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "", require_token_getter=lambda: True)
    assert await run_through(mw, make_scope(ip="192.168.1.50")) == 401
    assert await run_through(mw, make_scope(ip="127.0.0.1")) == 200, "loopback reste libre"


@pytest.mark.asyncio
async def test_dash_mutation_same_origin_ok_cross_blocked(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "")
    good = make_scope(
        path="/api/proxy/restart",
        method="POST",
        ip="192.168.1.50",
        headers={"Origin": "http://localhost:4000"},
    )
    assert await run_through(mw, good) == 200
    evil = make_scope(
        path="/api/proxy/restart",
        method="POST",
        ip="192.168.1.50",
        headers={"Origin": "https://evil.example"},
    )
    assert await run_through(mw, evil) == 403


@pytest.mark.asyncio
async def test_dash_mutation_rate_limited(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    limiter = _MutationRateLimiter(limit=2, window=60.0)
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "", rate_limiter=limiter)
    base = {"Origin": "http://localhost:4000"}
    assert (
        await run_through(
            mw, make_scope(method="POST", ip="192.168.1.50", headers=base)
        )
        == 200
    )
    assert (
        await run_through(
            mw, make_scope(method="POST", ip="192.168.1.50", headers=base)
        )
        == 200
    )
    assert (
        await run_through(
            mw, make_scope(method="POST", ip="192.168.1.50", headers=base)
        )
        == 429
    ), "3e mutation dans la fenêtre → 429"
    assert (
        await run_through(
            mw, make_scope(method="POST", ip="10.0.0.99", headers=base)
        )
        == 200
    ), "autre IP non impactée"


@pytest.mark.asyncio
async def test_dash_non_api_paths_bypassed(monkeypatch):
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "off"})
    mw = DashboardTrustMiddleware(None, token_getter=lambda: "")
    assert await run_through(mw, make_scope(path="/v1/messages", ip="203.0.113.9")) == 200
    assert await run_through(mw, make_scope(path="/static/app.js", ip="203.0.113.9")) == 200


# ── ASGI : ClientAuthMiddleware ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_default_none_is_passthrough(monkeypatch):
    set_cfg(monkeypatch, "client_auth", {"mode": "none"})
    mw = ClientAuthMiddleware(None)
    assert await run_through(mw, make_scope(path="/v1/messages", ip="203.0.113.9")) == 200


@pytest.mark.asyncio
async def test_client_lan_mode(monkeypatch):
    set_cfg(monkeypatch, "client_auth", {"mode": "lan"})
    mw = ClientAuthMiddleware(None)
    assert await run_through(mw, make_scope(path="/v1/messages", ip="192.168.1.7")) == 200
    assert await run_through(mw, make_scope(path="/v1/messages", ip="203.0.113.9")) == 403
    assert (
        await run_through(
            mw, make_scope(path="/api/circuit-breakers/reset", method="POST", ip="203.0.113.9")
        )
        == 403
    ), "endpoints reset protégés aussi"


@pytest.mark.asyncio
async def test_client_key_mode(monkeypatch):
    set_cfg(monkeypatch, "client_auth", {"mode": "key", "key": "sk-client-secret"})
    mw = ClientAuthMiddleware(None)
    plain = make_scope(path="/v1/messages", ip="203.0.113.9")
    assert await run_through(mw, plain) == 401
    bearer = make_scope(
        path="/v1/messages",
        ip="203.0.113.9",
        headers={"Authorization": "Bearer sk-client-secret"},
    )
    assert await run_through(mw, bearer) == 200
    xkey = make_scope(
        path="/v1/messages", ip="203.0.113.9", headers={"x-api-key": "sk-client-secret"}
    )
    assert await run_through(mw, xkey) == 200
    wrong = make_scope(
        path="/v1/messages", ip="203.0.113.9", headers={"Authorization": "Bearer nope"}
    )
    assert await run_through(mw, wrong) == 401


@pytest.mark.asyncio
async def test_client_key_without_secret_falls_back_to_lan(monkeypatch):
    warnings: list[str] = []
    set_cfg(monkeypatch, "client_auth", {"mode": "key", "key": ""})
    mw = ClientAuthMiddleware(None, warn=warnings.append)
    assert await run_through(mw, make_scope(path="/v1/messages", ip="192.168.1.7")) == 200
    assert await run_through(mw, make_scope(path="/v1/messages", ip="203.0.113.9")) == 403
    assert any("repli" in w for w in warnings)


# ── intégration : short-circuit v9 dans _check_dashboard_token ───────────


def test_check_dashboard_token_zero_friction_for_trusted(monkeypatch):
    import dashboard.api as api

    class FakeReq:
        def __init__(self, ip):
            from types import SimpleNamespace

            self.headers = {}
            self.client = SimpleNamespace(host=ip) if ip else None

    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "tok123", raising=False)
    monkeypatch.setattr(api, "_DASHBOARD_REQUIRE_TOKEN", False, raising=False)
    set_cfg(monkeypatch, "dashboard_trust", {"mode": "lan"})
    assert api._check_dashboard_token(FakeReq("127.0.0.1")) is None, "loopback sans friction"
    assert api._check_dashboard_token(FakeReq("192.168.1.50")) is None, "LAN sans friction"
    resp = api._check_dashboard_token(FakeReq("203.0.113.9"))
    assert resp is not None and resp.status_code == 401, "hors confiance → token exigé"

    monkeypatch.setattr(api, "_DASHBOARD_REQUIRE_TOKEN", True, raising=False)
    resp2 = api._check_dashboard_token(FakeReq("192.168.1.50"))
    # Token configuré + REQUIRE_TOKEN → 401 historique (sémantique F-H6 inchangée)
    assert resp2 is not None and resp2.status_code == 401, "require_token réapplique la contrainte"
