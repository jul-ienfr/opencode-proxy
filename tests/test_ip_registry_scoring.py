"""Scoring tests for the IP registry (Phase 0)."""

from __future__ import annotations

from free.ip_registry.domain import IpRecord, IpState
from free.ip_registry.scoring import ScoreWeights, score_record


def _record(**kwargs) -> IpRecord:
    return IpRecord(
        egress_ip="87.120.102.53",
        hostname="al109.nordvpn.com",
        country="Albania",
        **kwargs,
    )


def test_fresh_score_is_neutral():
    score = score_record(_record(), now=1000.0)
    assert 35.0 <= score <= 65.0


def test_good_record_scores_high():
    record = _record(
        state=IpState.ASSIGNED,
        tests=10,
        successes=10,
        latency_ms=500,
        last_tested_at=1000.0,
        last_ok_at=1000.0,
    )
    assert score_record(record, now=1000.0) > 70.0


def test_exhausted_before_reset_is_zero():
    record = _record(state=IpState.EXHAUSTED, reset_at=2000.0)
    assert score_record(record, now=1000.0) == 0.0


def test_exhausted_after_reset_is_neutral():
    record = _record(state=IpState.EXHAUSTED, reset_at=500.0)
    assert score_record(record, now=1000.0) == 40.0


def test_error_in_cooldown_is_low():
    record = _record(state=IpState.ERROR, error_until=2000.0)
    assert score_record(record, now=1000.0) == 5.0


def test_error_after_cooldown_is_neutral_low():
    record = _record(state=IpState.ERROR, error_until=500.0)
    assert score_record(record, now=1000.0) == 30.0


def test_score_is_bounded():
    record = _record(
        state=IpState.ASSIGNED,
        tests=1000,
        successes=1000,
        latency_ms=0,
        last_tested_at=1000.0,
    )
    weights = ScoreWeights(latency_budget_ms=8000)
    for load in (0.0, 100.0, 10_000.0):
        score = score_record(record, now=1000.0, weights=weights, server_load=load)
        assert 0.0 <= score <= 100.0


def test_weights_are_normalized():
    weights = ScoreWeights(availability=2.0, latency=1.0, freshness=1.0).normalized()
    assert abs(weights.availability + weights.latency + weights.freshness - 1.0) < 1e-9


def test_latency_penalty_lowers_score():
    fast = _record(
        state=IpState.ASSIGNED,
        tests=5,
        successes=5,
        latency_ms=200,
        last_tested_at=1000.0,
    )
    slow = _record(
        state=IpState.ASSIGNED,
        tests=5,
        successes=5,
        latency_ms=7900,
        last_tested_at=1000.0,
    )
    assert score_record(fast, now=1000.0) > score_record(slow, now=1000.0)
