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
    assert policy.active_count() == 2


def test_no_provider_is_degraded_fail_closed():
    policy = _policy()
    decision = policy.evaluate("s1", FallbackTrigger.WARP_DOWN, {"warp"}, now=100.0)
    assert decision.action == "degraded"
    assert decision.reason == "no_fallback_provider"
    assert policy.snapshot()["fail_closed"] is True


def test_return_to_warp_after_two_successes_and_cooldown():
    policy = _policy()
    policy.evaluate("s1", FallbackTrigger.QUOTA, {"warp", "proton"}, now=100.0)
    # Cooldown pas écoulé : les succès ne suffisent pas.
    d1 = policy.note_probe("s1", "proton", True, warp_healthy=True, now=200.0)
    d2 = policy.note_probe("s1", "proton", True, warp_healthy=True, now=300.0)
    assert d1.action == "stay"
    assert d2.action == "stay"
    # Après cooldown + 2 succès consécutifs.
    d3 = policy.note_probe("s1", "proton", True, warp_healthy=True, now=1101.0)
    assert d3.action == "return"
    assert d3.provider == "warp"
    assert policy.active_count() == 0


def test_probe_failure_resets_success_counter():
    policy = _policy()
    policy.evaluate("s1", FallbackTrigger.QUOTA, {"warp", "proton"}, now=100.0)
    policy.note_probe("s1", "proton", True, warp_healthy=True, now=1101.0)
    policy.note_probe("s1", "proton", False, warp_healthy=True, now=1102.0)
    d = policy.note_probe("s1", "proton", True, warp_healthy=True, now=1103.0)
    assert d.action == "stay"


def test_403_and_rate_limit_do_not_trigger_fallback():
    policy = _policy()
    for cls in ("GATE_403", "AUTH", "RATE_LIMIT_429", "TRANSIENT_5XX"):
        d = policy.evaluate_error("s1", cls, {"warp", "proton"}, now=100.0)
        assert d.action == "noop"
        assert d.reason == "not_a_quota"
    assert policy.active_count() == 0


def test_quota_error_triggers_and_snapshot():
    policy = _policy()
    d = policy.evaluate_error("s1", "QUOTA_429", {"warp", "proton"}, now=100.0)
    assert d.action == "fallback"
    snap = policy.snapshot()
    assert snap["active"] == {"s1": "proton"}
    assert snap["active_count"] == 1
