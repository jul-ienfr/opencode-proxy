"""Quotas trial-mort : le message 429 du provider pilote le cooldown.

Couvre : classification du scope (core.errors, pur), durées humaines du
corps en repli du header, cooldown long trial-mort + registre de scopes,
saut des clés mortes dans les picks (helper), exposition dashboard.
"""

import time
from types import SimpleNamespace

import pytest

import opencode as oc
from core.errors import free_quota_scope, parse_body_retry_seconds

REAL_TRIAL_BODY = (
    '{"type":"error","error":{"type":"FreeUsageLimitError","message":'
    '"Free usage limit reached. Subscribe to OpenCode Go to keep going: '
    'https://opencode.ai/go"}}'
)


def test_scope_trial_exhausted_real_body():
    assert free_quota_scope(429, REAL_TRIAL_BODY) == "trial-exhausted"


def test_scope_rate_limited():
    assert free_quota_scope(429, "Rate limit exceeded, slow down") == "rate-limited"
    assert free_quota_scope(429, '{"error":{"type":"ratelimiterror"}}') == "rate-limited"
    assert free_quota_scope(429, '{"error":{"type":"GoUsageLimitError"}}') == "rate-limited"


def test_scope_transient_and_unknown():
    assert free_quota_scope(429, "Endpoint is unavailable") == "transient"
    assert free_quota_scope(429, "") == "unknown"
    assert free_quota_scope(429, None) == "unknown"
    assert free_quota_scope(200, REAL_TRIAL_BODY) == "unknown"
    assert free_quota_scope("429?", REAL_TRIAL_BODY) == "unknown"


def test_body_duration_parsing():
    assert parse_body_retry_seconds("retry in 3hr 12min") == 3 * 3600 + 12 * 60
    assert parse_body_retry_seconds("quota resets in 2 days") is None  # > cap
    assert parse_body_retry_seconds("try again in 45min") == 2700.0
    assert parse_body_retry_seconds("back in 1 day") == 86400.0
    assert parse_body_retry_seconds("Free usage limit reached") is None
    assert parse_body_retry_seconds("") is None
    assert parse_body_retry_seconds(None) is None
    assert parse_body_retry_seconds("retry in 0min") is None


def test_cooldown_header_wins_over_body():
    assert oc._free_429_cooldown_seconds("90", "retry in 3hr 12min") == 90.0
    # [2026-11-06] Le corps « 3hr 12min » est plafonné à 20 min : ce n'est pas
    # une vraie recovery (cf. afree-proxy). Le plafond reste désactivable.
    assert oc._free_429_cooldown_seconds("", "retry in 3hr 12min") == 1200.0
    assert oc._free_429_cooldown_seconds("") == oc._FREE_429_DEFAULT


def test_cooldown_cap_applies_to_huge_retry_after(monkeypatch):
    monkeypatch.setattr(oc, "_free_cooldown_cap_seconds", lambda: 1200.0)
    # Retry-After ~19 h (distance à minuit UTC) -> 20 min effectives.
    assert oc._free_429_cooldown_seconds("67000") == 1200.0
    assert oc._free_429_cooldown_seconds("", "retry in 3hr 12min") == 1200.0


def test_cooldown_cap_can_be_disabled(monkeypatch):
    monkeypatch.setattr(oc, "_free_cooldown_cap_seconds", lambda: 0.0)
    assert oc._free_429_cooldown_seconds("67000") == 67000.0
    assert oc._free_429_cooldown_seconds("", "retry in 3hr 12min") == 3 * 3600 + 12 * 60


def _stub_station(ip="9.9.9.9"):
    class _St:
        def __init__(self, ip):
            self.current_ip = ip
            self.pid = None
            self._station = 99

    return _St(ip)


@pytest.fixture
def _clean_ledger():
    keys = [k for k in oc._free_model_cooldowns if k.startswith("test-trial-")]
    for k in keys:
        oc._free_model_cooldowns.pop(k, None)
        oc._free_cooldown_scope.pop(k, None)
    yield
    for k in [k for k in oc._free_model_cooldowns if k.startswith("test-trial-")]:
        oc._free_model_cooldowns.pop(k, None)
        oc._free_cooldown_scope.pop(k, None)


def test_trial_dead_long_cooldown_and_ledger(_clean_ledger):
    st = _stub_station("10.99.0.1")
    oc._set_free_cooldown("test-trial-m", 60.0, st, scope="trial-exhausted")
    assert oc._free_trial_dead_active("test-trial-m", st) is True
    assert oc._free_cooldown_active("test-trial-m", st) is True
    # Une décision fraîche sans scope remplace l'ancien scope.
    oc._set_free_cooldown("test-trial-m", 60.0, st)
    assert oc._free_trial_dead_active("test-trial-m", st) is False
    assert oc._free_cooldown_active("test-trial-m", st) is True


def test_trial_dead_expiry_purges(_clean_ledger):
    st = _stub_station("10.99.0.2")
    oc._set_free_cooldown("test-trial-m", 60.0, st, scope="trial-exhausted")
    key = oc._free_cooldown_key("test-trial-m", st)
    oc._free_model_cooldowns[key] = time.monotonic() - 1.0  # expiré à la main
    assert oc._free_trial_dead_active("test-trial-m", st) is False
    assert key not in oc._free_model_cooldowns
    assert key not in oc._free_cooldown_scope


def test_socks5_cooldown_key_uses_real_egress_not_label():
    from free.pool import Socks5Endpoint

    configured = Socks5Endpoint(
        "127.0.0.1",
        11080,
        label="warp-ipv6",
        ipv6=True,
        egress_ip="2a09:bac5:32d4:be::13:23e",
    )
    assert oc._free_cooldown_key("test-m", configured) == "test-m|2a09:bac5:32d4:be"
    unprobed = Socks5Endpoint("127.0.0.1", 11085, label="warp-ipv6-2", ipv6=True)
    assert oc._free_cooldown_key("test-m", unprobed) == "test-m|socks5:warp-ipv6-2"


def test_trial_dead_stations_helper(monkeypatch, _clean_ledger):
    live = _stub_station("10.99.0.10")
    dead = _stub_station("10.99.0.11")
    pool = SimpleNamespace(_stations=[live, dead], socks5_mode=False)
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    oc._set_free_cooldown("test-trial-m", 3600.0, dead, scope="trial-exhausted")
    oc._set_free_cooldown("test-trial-m", 60.0, live, scope="rate-limited")
    got = oc._trial_dead_stations("test-trial-m")
    # [audit 2026-10-30] ANY active (model, IP) cooldown is skipped, not only
    # the long trial-exhausted scope: a rate-limited station must not be
    # re-struck and re-arm its timer.
    assert got == {dead, live}


def test_quota_states_dashboard_shape(_clean_ledger):
    st = _stub_station("10.99.0.20")
    oc._set_free_cooldown("test-trial-m", 3600.0, st, scope="trial-exhausted")
    states = oc._free_quota_states()
    mine = [s for s in states if s["model"] == "test-trial-m"]
    assert len(mine) == 1
    s = mine[0]
    assert s["ip"] == "10.99.0.20"
    assert s["scope"] == "trial-exhausted"
    assert 0 < s["retry_in_s"] <= 3600.0
    assert s["retry_at"].endswith("Z")
