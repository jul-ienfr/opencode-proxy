"""Eligibility tests for the IP registry (Phase 0)."""

from __future__ import annotations

from free.ip_registry.domain import GeoFilter, IpRecord, IpState
from free.ip_registry.eligibility import check_eligibility
from free.ip_registry.scoring import ScoreWeights


def _record(**kwargs) -> IpRecord:
    return IpRecord(
        egress_ip="87.120.102.53",
        hostname="al109.nordvpn.com",
        country="Albania",
        **kwargs,
    )


def test_fresh_is_eligible():
    decision = check_eligibility(_record(), now=1000.0)
    assert decision.eligible, decision.reason


def test_assigned_is_not_eligible():
    decision = check_eligibility(_record(state=IpState.ASSIGNED, assigned_station=6), now=1000.0)
    assert not decision.eligible
    assert decision.reason == "already-assigned"


def test_leased_is_not_eligible():
    record = _record(state=IpState.CLAIMED, lease_id="l1", lease_expires_at=2000.0)
    decision = check_eligibility(record, now=1000.0)
    assert not decision.eligible
    assert decision.reason == "leased"


def test_exhausted_before_reset_is_not_eligible():
    record = _record(state=IpState.EXHAUSTED, reset_at=2000.0)
    decision = check_eligibility(record, now=1000.0)
    assert not decision.eligible
    assert decision.reason == "quota-reset-pending"


def test_exhausted_after_reset_is_eligible():
    record = _record(state=IpState.EXHAUSTED, reset_at=500.0)
    decision = check_eligibility(record, now=1000.0)
    assert decision.eligible, decision.reason


def test_error_before_cooldown_is_not_eligible():
    record = _record(state=IpState.ERROR, error_until=2000.0)
    decision = check_eligibility(record, now=1000.0)
    assert not decision.eligible
    assert decision.reason == "error-cooldown"


def test_geo_filter_rejects_other_country():
    geo = GeoFilter(countries=frozenset({"France"}), require=True)
    decision = check_eligibility(_record(), now=1000.0, geo=geo)
    assert not decision.eligible
    assert decision.reason == "geo-filtered"


def test_geo_filter_accepts_allowed_country():
    geo = GeoFilter(countries=frozenset({"Albania"}), require=True)
    decision = check_eligibility(_record(), now=1000.0, geo=geo)
    assert decision.eligible, decision.reason


def test_min_score_blocks_low_score():
    record = _record(state=IpState.CANDIDATE, tests=10, successes=0, last_tested_at=1000.0)
    decision = check_eligibility(
        record,
        now=1000.0,
        weights=ScoreWeights(min_score=99.0),
    )
    assert not decision.eligible
    assert decision.reason is not None and decision.reason.startswith("score-below-")
