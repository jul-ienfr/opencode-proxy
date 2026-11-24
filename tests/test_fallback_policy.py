"""FallbackPolicy 007 : ordre gratuit, max 2, retour WARP, 403 non-déclencheur."""

from __future__ import annotations

from free.fallback import FallbackConfig, FallbackPolicy, FallbackTrigger


def _policy(**kwargs) -> FallbackPolicy:
    cfg = FallbackConfig.from_mapping(
        {
            "order": ["warp", "warp_alt", "proton", "windscribe"],
            "max_fallback_stations": 2,
            "recovery_successes": 2,
            "cooldown_s": 900,
            **kwargs,
        }
    )
    return FallbackPolicy(cfg)


def test_select_provider_skips_warp_and_follows_order():
    policy = _policy()
    assert policy.select_provider({"warp", "warp_alt", "proton"}) == "warp_alt"
    assert policy.select_provider({"warp", "proton"}) == "proton"
    assert policy.select_provider({"warp"}) is None


def test_max_two_fallback_stations():
    policy = _policy()
    available = {"warp", "proton", "windscribe"}
    d1 = policy.evaluate("s1", FallbackTrigger.QUOTA, available, now=100.0)
    d2 = policy.evaluate("s2", FallbackTrigger.QUOTA, available, now=101.0)
    d3 = policy.evaluate("s3", FallbackTrigger.QUOTA, available, now=102.0)
    assert d1.action == "fallback" and d1.provider == "proton"
    assert d2.action == "fallback" and d2.provider == "proton"
    assert d3.action == "degraded"
    assert d3.reason == "max_fallback_stations"
    assert policy.snapshot()["active_count"] == 2


def test_no_provider_is_degraded_fail_closed():
    policy = _policy()
    decision = policy.evaluate("s1", FallbackTrigger.WARP_DOWN, {"warp"}, now=100.0)
    assert decision.action == "degraded"
    assert decision.reason == "no_fallback_provider"
    assert policy.snapshot()["fail_closed"] is True


def test_quota_error_triggers_and_snapshot():
    policy = _policy()
    d = policy.evaluate("s1", FallbackTrigger.QUOTA, {"warp", "proton"}, now=100.0)
    assert d.action == "fallback"
    snap = policy.snapshot()
    assert snap["active"] == {"s1": "proton"}
    assert snap["active_count"] == 1
