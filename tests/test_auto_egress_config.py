"""Auto-Egress 008 : checkboxes providers → mode dérivé + legacy."""

from __future__ import annotations

import pytest

from free.auto_egress import (
    PROVIDERS,
    SmoothWeightedRoundRobin,
    derive_mode,
    equal_weights,
    normalize_providers,
    normalize_weights,
    providers_from_mode,
)


def test_normalize_dedupes_and_orders():
    assert normalize_providers(["warp", "nordvpn", "warp", "unknown"]) == [
        "warp",
        "nordvpn",
    ]


def test_normalize_preserves_user_order():
    assert normalize_providers(["proxies", "nordvpn", "warp"]) == [
        "proxies",
        "nordvpn",
        "warp",
    ]


def test_normalize_accepts_comma_string():
    assert normalize_providers("warp,proxies") == ["warp", "proxies"]


def test_normalize_allows_direct_with_others():
    assert normalize_providers(["direct", "warp"]) == ["direct", "warp"]
    assert normalize_providers(["nordvpn", "direct"]) == ["nordvpn", "direct"]


def test_derive_mode_single_and_multi():
    assert derive_mode(["nordvpn"]) == "vpn"
    assert derive_mode(["warp"]) == "socks5"
    assert derive_mode(["proxies"]) == "socks5"
    assert derive_mode(["warp", "proxies"]) == "socks5"
    assert derive_mode(["direct"]) == "direct"
    assert derive_mode(["nordvpn", "warp"]) == "auto_egress"
    assert derive_mode(["nordvpn", "proxies"]) == "auto_egress"
    assert derive_mode(["nordvpn", "direct"]) == "auto_egress"
    assert derive_mode(["warp", "direct"]) == "socks5"
    assert derive_mode(["nordvpn", "warp", "proxies", "direct"]) == "auto_egress"


def test_derive_mode_empty_raises():
    with pytest.raises(ValueError):
        derive_mode([])


def test_legacy_mode_to_providers():
    assert providers_from_mode("vpn") == ["nordvpn"]
    assert providers_from_mode("socks5") == ["warp"]
    assert providers_from_mode("direct") == ["direct"]
    assert providers_from_mode("auto_egress") == ["nordvpn", "warp"]
    assert providers_from_mode("") == ["nordvpn"]


def test_provider_list_is_stable():
    assert PROVIDERS == ("nordvpn", "warp", "proxies", "direct")


def test_provider_matching_for_warp_and_proxies():
    from free.pool import FreeIPPool, Socks5Endpoint

    pool = object.__new__(FreeIPPool)
    warp = Socks5Endpoint("127.0.0.1", 11080, label="w", provider="warp")
    managed_default = Socks5Endpoint("127.0.0.1", 11081, label="m", managed=True)
    static = Socks5Endpoint("127.0.0.1", 1080, label="p", provider="static")
    assert pool._provider_matches(warp, "warp") is True
    assert pool._provider_matches(managed_default, "warp") is True
    assert pool._provider_matches(static, "warp") is False
    assert pool._provider_matches(static, "proxies") is True
    assert pool._provider_matches(warp, "proxies") is False


def test_equal_weights_for_two_three_four():
    assert equal_weights(["nordvpn", "warp"]) == {"nordvpn": 50, "warp": 50}
    assert equal_weights(["nordvpn", "warp", "proxies"]) == {
        "nordvpn": 34,
        "warp": 33,
        "proxies": 33,
    }
    assert equal_weights(["nordvpn", "warp", "proxies", "direct"]) == {
        "nordvpn": 25,
        "warp": 25,
        "proxies": 25,
        "direct": 25,
    }


def test_normalize_weights_sums_to_100():
    assert normalize_weights({"nordvpn": 2, "warp": 1}, ["nordvpn", "warp"]) == {
        "nordvpn": 67,
        "warp": 33,
    }
    assert normalize_weights({"nordvpn": 0, "warp": 0}, ["nordvpn", "warp"]) == {
        "nordvpn": 50,
        "warp": 50,
    }
    assert sum(normalize_weights({"a": 1, "b": 1, "c": 1}, ["a", "b", "c"]).values()) == 100


def test_smooth_weighted_round_robin_distribution():
    scheduler = SmoothWeightedRoundRobin()
    weights = {"nordvpn": 50, "warp": 30, "proxies": 20}
    counts = {"nordvpn": 0, "warp": 0, "proxies": 0}
    for _ in range(100):
        counts[scheduler.pick(weights)] += 1
    assert counts == {"nordvpn": 50, "warp": 30, "proxies": 20}


def test_smooth_weighted_round_robin_zero_and_single():
    scheduler = SmoothWeightedRoundRobin()
    assert scheduler.pick({"nordvpn": 0, "warp": 0}) is None
    assert scheduler.pick({"nordvpn": 100, "warp": 0}) == "nordvpn"


def test_fallback_selects_providers_in_declared_order():
    from free.pool import FreeIPPool

    pool = object.__new__(FreeIPPool)
    pool._egress_strategy = "fallback"
    pool._egress_weights = {"nordvpn": 100, "warp": 0}
    pool._egress_swrr = SmoothWeightedRoundRobin()
    assert pool._select_egress_provider(["nordvpn", "warp"]) == "nordvpn"
    # Le premier indisponible → le deuxième seulement.
    assert pool._select_egress_provider(["warp"]) == "warp"
    assert pool._select_egress_provider([]) is None


def test_balance_respects_weights_and_renormalizes_down_provider():
    from free.pool import FreeIPPool

    pool = object.__new__(FreeIPPool)
    pool._egress_strategy = "balance"
    pool._egress_swrr = SmoothWeightedRoundRobin()
    pool._egress_weights = {"nordvpn": 50, "warp": 30, "proxies": 20}
    counts = {"nordvpn": 0, "warp": 0, "proxies": 0}
    for _ in range(100):
        counts[pool._select_egress_provider(["nordvpn", "warp", "proxies"])] += 1
    assert counts == {"nordvpn": 50, "warp": 30, "proxies": 20}

    # WARP down : 50/20 renormalisé en 71/29, sans division par zéro.
    pool._egress_swrr.reset()
    picks = [pool._select_egress_provider(["nordvpn", "proxies"]) for _ in range(100)]
    assert picks.count("nordvpn") == 71
    assert picks.count("proxies") == 29


def test_balance_zero_weight_is_standby_only():
    from free.pool import FreeIPPool

    pool = object.__new__(FreeIPPool)
    pool._egress_strategy = "balance"
    pool._egress_swrr = SmoothWeightedRoundRobin()
    pool._egress_weights = {"nordvpn": 100, "warp": 0}
    assert {pool._select_egress_provider(["nordvpn", "warp"]) for _ in range(10)} == {
        "nordvpn"
    }
    # Plus aucun provider à poids positif : le secours sert.
    assert pool._select_egress_provider(["warp"]) == "warp"


def test_balance_four_providers_equal_parts():
    from free.pool import FreeIPPool

    pool = object.__new__(FreeIPPool)
    pool._egress_strategy = "balance"
    pool._egress_swrr = SmoothWeightedRoundRobin()
    pool._egress_weights = {"nordvpn": 25, "warp": 25, "proxies": 25, "direct": 25}
    providers = ["nordvpn", "warp", "proxies", "direct"]
    counts = dict.fromkeys(providers, 0)
    for _ in range(100):
        counts[pool._select_egress_provider(providers)] += 1
    assert counts == {"nordvpn": 25, "warp": 25, "proxies": 25, "direct": 25}


def test_order_prefers_socks5_when_first_provider_is_warp():
    from free.pool import FreeIPPool

    pool = object.__new__(FreeIPPool)
    pool._egress_providers = ["warp", "nordvpn"]
    assert pool._egress_order_prefers_socks5() is True
    pool._egress_providers = ["nordvpn", "warp"]
    assert pool._egress_order_prefers_socks5() is False
    pool._egress_providers = ["proxies", "nordvpn"]
    assert pool._egress_order_prefers_socks5() is True
    pool._egress_providers = ["warp"]
    assert pool._egress_order_prefers_socks5() is False
