"""Scoring tests: potential vs measured, no country/source factors."""

from __future__ import annotations

from free.qualification.domain import Candidate
from free.qualification.scoring import (
    MeasuredWeights,
    PotentialWeights,
    measured_score,
    potential_score,
)


def _candidate(**changes) -> Candidate:
    base = {
        "hostname": "se1.nordvpn.com",
        "country": "Sweden",
        "load": 10.0,
        "rtt_ms": 20.0,
        "quota_remaining": 450,
        "success_count": 0,
        "fail_count": 0,
        "last_seen_at": 100.0,
        "first_seen_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_potential_is_bounded_and_monotonic():
    now = 100.0
    fast = _candidate(rtt_ms=10.0)
    slow = _candidate(rtt_ms=200.0)
    assert 0.0 <= potential_score(slow, now=now) <= 100.0
    assert potential_score(fast, now=now) > potential_score(slow, now=now)


def test_country_and_source_are_not_factors():
    now = 100.0
    a = _candidate(country="Sweden", source_id="api")
    b = _candidate(country="Japan", source_id="embedded")
    assert potential_score(a, now=now) == potential_score(b, now=now)


def test_quota_reset_zeroes_quota_component():
    now = 100.0
    fresh = _candidate(quota_remaining=450, quota_reset_at=None)
    reset = _candidate(quota_remaining=450, quota_reset_at=200.0)
    assert potential_score(fresh, now=now) > potential_score(reset, now=now)


def test_weights_normalize():
    weights = PotentialWeights(reliability=2, latency=2, quota=2, load=2, freshness=2).normalized()
    assert abs(weights.reliability - 0.2) < 1e-9
    assert abs(weights.latency - 0.2) < 1e-9


def test_measured_score_uses_ix_weights():
    candidate = _candidate(success_count=3, fail_count=1, last_attempt_at=100.0)
    score = measured_score(candidate, now=100.0, latency_ms=1000.0)
    assert 0.0 <= score <= 100.0
    perfect = _candidate(success_count=10, fail_count=0, last_attempt_at=100.0)
    assert measured_score(perfect, now=100.0, latency_ms=0.0) > score


def test_measured_weights_normalize():
    weights = MeasuredWeights(success=4, latency=4, throughput=4, stability=4, freshness=4).normalized()
    assert abs(weights.success - 0.2) < 1e-9
