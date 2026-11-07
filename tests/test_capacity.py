"""Tests 010 — capacité par provider et allocation proportionnelle."""

from __future__ import annotations

import pytest

from free.capacity import (
    capacity_config,
    effective_policy,
    evaluate_capacity,
    proportional_allocation,
)


def _counts(nordvpn=0, warp=0, proxies=0, direct=1):
    return {
        "nordvpn": {"configured": nordvpn, "enabled": nordvpn, "usable": nordvpn},
        "warp": {"configured": warp, "enabled": warp, "usable": warp},
        "proxies": {"configured": proxies, "enabled": proxies, "usable": proxies},
        "direct": {"configured": 1, "enabled": 1, "usable": direct},
    }


def _report(**kwargs):
    defaults = {
        "providers": _counts(6, 6),
        "selected": ["nordvpn", "warp"],
        "strategy": "fallback",
        "weights": {"nordvpn": 50, "warp": 50},
        "target_slots": 6,
        "reserve_policy": "full",
    }
    defaults.update(kwargs)
    return evaluate_capacity(**defaults)


def test_capacity_config_defaults_and_clamp():
    assert capacity_config({}) == (6, "full")
    assert capacity_config({"target_slots": 0}) == (1, "full")
    assert capacity_config({"target_slots": 99}) == (10, "full")
    assert capacity_config({"target_slots": 6, "reserve_policy": "nope"}) == (
        6,
        "full",
    )
    assert capacity_config({"reserve_policy": "shared"}) == (6, "shared")


def test_effective_policy_coerces_proportional_in_fallback():
    assert effective_policy("balance", "proportional") == ("proportional", False)
    assert effective_policy("fallback", "proportional") == ("shared", True)


def test_proportional_allocation_50_50():
    assert proportional_allocation(6, {"nordvpn": 50, "warp": 50}) == {
        "nordvpn": 3,
        "warp": 3,
    }


def test_proportional_allocation_two_to_one():
    assert proportional_allocation(6, {"nordvpn": 67, "warp": 33}) == {
        "nordvpn": 4,
        "warp": 2,
    }


def test_proportional_allocation_three_to_one_prefers_even_load():
    # 4+2 (18,75 % / 12,5 %) plutôt que 5+1 (15 % / 25 %).
    assert proportional_allocation(6, {"nordvpn": 75, "warp": 25}) == {
        "nordvpn": 4,
        "warp": 2,
    }


def test_proportional_allocation_three_equal():
    assert proportional_allocation(
        6, {"nordvpn": 34, "warp": 33, "proxies": 33}
    ) == {"nordvpn": 2, "warp": 2, "proxies": 2}


def test_proportional_allocation_40_40_20():
    allocation = proportional_allocation(
        6, {"nordvpn": 40, "warp": 40, "proxies": 20}
    )
    assert sorted(allocation.values()) == [1, 2, 3]
    assert sum(allocation.values()) == 6


def test_proportional_allocation_direct_fixed():
    assert proportional_allocation(
        6,
        {"nordvpn": 50, "warp": 50},
        {"direct": 1},
    ) == {"direct": 1, "nordvpn": 3, "warp": 2}


def test_full_6_6_ok():
    report = _report()
    assert report.verdict == "ok"
    assert report.total_usable == 12
    assert report.total_required == 12
    assert report.deficit == 0
    assert not [w for w in report.warnings if w["code"] == "full_degraded"]


def test_full_6_3_degraded():
    report = _report(providers=_counts(6, 3))
    assert report.verdict == "degraded"
    assert report.providers["warp"].deficit == 3
    assert any(w["code"] == "full_degraded" for w in report.warnings)


def test_full_3_3_degraded():
    report = _report(providers=_counts(3, 3))
    assert report.verdict == "degraded"


def test_shared_3_3_ok_with_no_reserve_warning():
    report = _report(
        providers=_counts(3, 3),
        strategy="balance",
        reserve_policy="shared",
    )
    assert report.verdict == "ok"
    assert any(w["code"] == "no_reserve" for w in report.warnings)


def test_shared_6_6_no_reserve_warning():
    report = _report(
        providers=_counts(6, 6),
        strategy="balance",
        reserve_policy="shared",
    )
    assert report.verdict == "ok"
    assert not any(w["code"] == "no_reserve" for w in report.warnings)


def test_shared_2_3_insufficient():
    report = _report(
        providers=_counts(2, 3),
        strategy="balance",
        reserve_policy="shared",
    )
    assert report.verdict == "insufficient"
    assert report.total_usable == 5
    assert report.deficit == 1
    assert any(r["code"] == "add_endpoints" for r in report.recommendations)


def test_proportional_4_2_ok():
    report = _report(
        providers=_counts(4, 2),
        strategy="balance",
        weights={"nordvpn": 67, "warp": 33},
        reserve_policy="proportional",
    )
    assert report.verdict == "ok"
    assert report.providers["nordvpn"].required == 4
    assert report.providers["warp"].required == 2
    assert report.providers["nordvpn"].per_endpoint == pytest.approx(16.75)
    assert report.providers["warp"].per_endpoint == pytest.approx(16.5)


def test_direct_single_ip_warning():
    report = _report(
        providers=_counts(6, 6, direct=1),
        selected=["nordvpn", "warp", "direct"],
        strategy="balance",
        weights={"nordvpn": 45, "warp": 45, "direct": 10},
        reserve_policy="shared",
    )
    assert any(w["code"] == "direct_single_ip" for w in report.warnings)


def test_endpoint_imbalance_suggests_reweight():
    report = _report(
        providers=_counts(6, 3),
        strategy="balance",
        weights={"nordvpn": 50, "warp": 50},
        reserve_policy="shared",
    )
    assert any(w["code"] == "endpoint_imbalance" for w in report.warnings)
    suggestion = next(
        r for r in report.recommendations if r["code"] == "reweight_suggestion"
    )
    assert "67" in suggestion["fr"]
    assert "33" in suggestion["fr"]


def test_proportional_in_fallback_is_coerced():
    report = _report(reserve_policy="proportional")
    assert report.reserve_policy == "shared"
    assert report.requested_policy == "proportional"
    assert any(w["code"] == "proportional_fallback" for w in report.warnings)


def test_unavailable_provider_warning():
    report = _report(providers=_counts(6, 0))
    assert report.providers["warp"].status == "unavailable"
    assert any(w["code"] == "unavailable_warp" for w in report.warnings)


def test_observed_share_and_warning():
    report = _report(
        strategy="balance",
        observed={"nordvpn": 90, "warp": 10},
    )
    assert report.observed_requests == 100
    assert report.providers["nordvpn"].observed_share == 90.0
    assert report.providers["warp"].observed_share == 10.0
    assert any(w["code"] == "observed_shift" for w in report.warnings)


def test_observed_no_warning_under_sample():
    report = _report(
        strategy="balance",
        observed={"nordvpn": 9, "warp": 1},
    )
    assert report.observed_requests == 10
    assert not any(w["code"] == "observed_shift" for w in report.warnings)


def test_observed_ignores_unselected_provider():
    report = _report(
        selected=["nordvpn"],
        observed={"nordvpn": 10, "warp": 90},
    )
    assert report.observed_requests == 10
    assert report.providers["nordvpn"].observed_share == 100.0
    assert report.providers["warp"].observed == 0
    assert report.providers["warp"].observed_share is None


def test_endpoint_hotspot_warning():
    report = _report(
        strategy="balance",
        endpoint_observed=[
            {"provider": "nordvpn", "endpoint": "s1", "requests": 10},
            {"provider": "warp", "endpoint": "w1", "requests": 10},
            {"provider": "warp", "endpoint": "w2", "requests": 40},
        ],
    )
    assert any(w["code"] == "endpoint_hotspot" for w in report.warnings)
    assert any(r["code"] == "endpoint_reweight" for r in report.recommendations)


def test_endpoint_hotspot_no_warning_below_threshold():
    report = _report(
        strategy="balance",
        endpoint_observed=[
            {"provider": "nordvpn", "endpoint": "s1", "requests": 10},
            {"provider": "warp", "endpoint": "w1", "requests": 10},
            {"provider": "warp", "endpoint": "w2", "requests": 15},
        ],
    )
    assert not any(w["code"] == "endpoint_hotspot" for w in report.warnings)


def test_endpoint_hotspot_ignores_unselected_provider():
    report = _report(
        selected=["warp"],
        strategy="balance",
        endpoint_observed=[
            {"provider": "nordvpn", "endpoint": "s1", "requests": 100},
            {"provider": "warp", "endpoint": "w1", "requests": 10},
            {"provider": "warp", "endpoint": "w2", "requests": 10},
        ],
    )
    assert not any(w["code"] == "endpoint_hotspot" for w in report.warnings)


def test_endpoint_inventory_maps_toggle_indices():
    from free.pool import FreeIPPool, Socks5Endpoint

    pool = object.__new__(FreeIPPool)
    pool._socks5_eps = [
        Socks5Endpoint("127.0.0.1", 11080, index=0, label="w1", provider="warp"),
        Socks5Endpoint("127.0.0.1", 1080, index=1, label="p1", provider="static"),
    ]
    inventory = pool.endpoint_inventory()
    assert [row["index"] for row in inventory] == [0, 1]
    assert [row["provider"] for row in inventory] == ["warp", "proxies"]
    assert inventory[0]["label"] == "w1"


def test_single_provider_warns_no_redundancy():
    report = _report(
        providers=_counts(0, 6),
        selected=["warp"],
    )
    assert report.verdict == "ok"
    assert any(w["code"] == "single_provider" for w in report.warnings)


def test_two_providers_no_single_provider_warning():
    report = _report()
    assert not any(w["code"] == "single_provider" for w in report.warnings)
