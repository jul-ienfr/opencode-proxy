"""Pure domain tests for the egress catalog."""

from __future__ import annotations

from free.egress_catalog import (
    EgressOption,
    Observation,
    ObservationPhase,
    OptionStatus,
    Provider,
    ProviderPolicy,
    canonical_key,
    classify_result,
    is_selectable,
    new_option,
    option_block,
    reset_from_retry_after,
    score_option,
    transition,
)


def _policy(provider: str = Provider.PUBLIC) -> ProviderPolicy:
    return ProviderPolicy(provider=provider, ttl_usable_s=100.0, ttl_quota_max_s=500.0, ttl_dead_s=900.0)


def _option(**changes) -> EgressOption:
    base = {
        "provider": Provider.PUBLIC,
        "kind": "https",
        "host": "1.2.3.4",
        "port": 443,
        "egress_ip": "1.2.3.4",
        "now": 1_000.0,
    }
    base.update(changes)
    return new_option(**base)


def test_public_block_is_the_egress_ip():
    assert option_block(Provider.PUBLIC, "1.2.3.4") == "1.2.3.4"
    assert option_block(Provider.NORDVPN, "1.2.3.4") == "1.2.3.4"


def test_warp_block_is_the_ipv6_slash_64():
    value = option_block(Provider.WARP, "2a09:bac5:32d2:26b4::3db:3c")
    assert value == "2a09:bac5:32d2:26b4::/64"


def test_canonical_key_distinguishes_endpoints_but_store_dedupes_egress():
    first = canonical_key(Provider.PUBLIC, "https", host="1.2.3.4", port=443, egress_ip="1.2.3.4")
    second = canonical_key(Provider.PUBLIC, "https", host="1.2.3.4", port=8443, egress_ip="1.2.3.4")
    assert first != second


def test_200_makes_the_option_usable_with_a_ttl():
    policy = _policy()
    option = _option()
    observed = transition(
        option,
        Observation(phase=ObservationPhase.FREE_GATE, status=200, result="usable", observed_at=1_000.0),
        policy,
    )
    assert observed.status == OptionStatus.USABLE
    assert observed.success_count == 1
    assert observed.expires_at == 1_100.0
    assert observed.last_checked_at == 1_000.0


def test_429_is_quota_with_the_provider_reset_not_dead():
    policy = _policy()
    observed = transition(
        _option(),
        Observation(phase=ObservationPhase.FREE_GATE, status=429, result="quota", observed_at=1_000.0),
        policy,
        reset_at=2_000.0,
    )
    assert observed.status == OptionStatus.QUOTA
    assert observed.reset_at == 2_000.0
    assert observed.expires_at == 2_000.0
    assert observed.fail_count == 1


def test_timeout_is_dead_with_a_short_lived_ttl():
    observed = transition(
        _option(),
        Observation(phase=ObservationPhase.LIVENESS, status=0, result="dead", observed_at=1_000.0),
        _policy(),
    )
    assert observed.status == OptionStatus.DEAD
    assert observed.expires_at == 1_900.0


def test_selectable_requires_usable_not_reachable():
    policy = _policy()
    reachable = _option().with_updates(
        status=OptionStatus.REACHABLE,
        expires_at=2_000.0,
    )
    usable = reachable.with_updates(status=OptionStatus.USABLE)
    assert not is_selectable(reachable, 1_100.0, policy)
    assert is_selectable(usable, 1_100.0, policy)


def test_selectable_rejects_quota_before_reset_and_expired():
    policy = _policy()
    quota = _option().with_updates(
        status=OptionStatus.QUOTA,
        expires_at=2_000.0,
        reset_at=1_500.0,
    )
    assert not is_selectable(quota, 1_100.0, policy)
    assert not is_selectable(quota, 1_600.0, policy)  # still quota status
    expired = _option().with_updates(status=OptionStatus.USABLE, expires_at=1_050.0)
    assert not is_selectable(expired, 1_100.0, policy)


def test_selectable_rejects_a_claimed_block():
    policy = _policy()
    usable = _option().with_updates(status=OptionStatus.USABLE, expires_at=2_000.0)
    assert not is_selectable(usable, 1_100.0, policy, claimed_blocks={usable.block})


def test_score_prefers_success_and_low_latency():
    policy = _policy()
    good = _option().with_updates(
        status=OptionStatus.USABLE,
        success_count=10,
        fail_count=0,
        last_latency_ms=200.0,
        expires_at=2_000.0,
    )
    bad = good.with_updates(success_count=1, fail_count=9, last_latency_ms=6_000.0)
    assert score_option(good, 1_100.0, policy) > score_option(bad, 1_100.0, policy)


def test_classify_result_never_turns_429_into_dead():
    assert classify_result(429) == OptionStatus.QUOTA
    assert classify_result(200) == OptionStatus.USABLE
    assert classify_result(0) == OptionStatus.DEAD
    assert classify_result(500) == OptionStatus.DEAD
    assert classify_result(401, phase=ObservationPhase.AUTH) == OptionStatus.AUTH


def test_reset_from_retry_after_requires_a_real_delay():
    assert reset_from_retry_after(0, now=1_000.0) == 0.0
    assert reset_from_retry_after(60, now=1_000.0) == 1_060.0


def test_policy_reads_config_and_clamps():
    policy = ProviderPolicy.from_config(
        Provider.PUBLIC,
        {"max_active": 8, "max_options": 0, "concurrency": 999, "ttl_usable_s": -3},
    )
    assert policy.max_active == 8
    assert policy.max_options == 1
    assert policy.concurrency == 999
    assert policy.ttl_usable_s == 0.0
