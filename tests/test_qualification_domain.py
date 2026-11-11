"""Pure domain tests for the qualification pipeline."""

from __future__ import annotations

from free.qualification.domain import (
    AttemptOutcome,
    AttemptStatus,
    Candidate,
    CandidateStatus,
    classify_outcome,
    infra_backoff,
    is_claimable,
    next_candidate_status,
    order_key,
)


def _candidate(**changes) -> Candidate:
    base = {
        "hostname": "se1.nordvpn.com",
        "transports": ("wireguard", "openvpn-udp", "openvpn-tcp"),
        "potential_score": 80.0,
        "quota_remaining": 400,
        "rtt_ms": 20.0,
        "load": 10.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_200_is_ok_and_promotes():
    outcome = AttemptOutcome(status=200, egress_ip="1.2.3.4", valid=True)
    attempt = classify_outcome(outcome)
    assert attempt is AttemptStatus.OK
    assert next_candidate_status(CandidateStatus.PENDING, attempt, now=100.0) is CandidateStatus.USABLE


def test_429_is_quota_never_dead():
    assert classify_outcome(AttemptOutcome(status=429)) is AttemptStatus.QUOTA
    assert next_candidate_status(CandidateStatus.PENDING, AttemptStatus.QUOTA, now=100.0) is CandidateStatus.QUOTA


def test_infra_is_pending_or_skipped_never_dead():
    for status in (AttemptStatus.NO_EGRESS, AttemptStatus.BIND_ERROR, AttemptStatus.TIMEOUT):
        assert next_candidate_status(CandidateStatus.PENDING, status, now=100.0) is CandidateStatus.PENDING
    assert (
        next_candidate_status(CandidateStatus.PENDING, AttemptStatus.NO_EGRESS, now=100.0, infra_failures=3)
        is CandidateStatus.SKIPPED
    )


def test_auth_is_station_level_not_host_death():
    assert classify_outcome(AttemptOutcome(status=401)) is AttemptStatus.AUTH
    assert (
        next_candidate_status(CandidateStatus.PENDING, AttemptStatus.AUTH, now=100.0)
        is CandidateStatus.PENDING
    )


def test_200_without_proof_is_dead():
    assert classify_outcome(AttemptOutcome(status=200, valid=False)) is AttemptStatus.DEAD


def test_order_is_potential_quota_rtt_load_hostname():
    a = _candidate(hostname="a", potential_score=90.0)
    b = _candidate(hostname="b", potential_score=80.0)
    assert order_key(a) < order_key(b)
    c = _candidate(hostname="c", potential_score=90.0, quota_remaining=500)
    assert order_key(c) < order_key(a)
    d = _candidate(hostname="d", potential_score=90.0, rtt_ms=10.0)
    assert order_key(d) < order_key(a)


def test_claimable_gates():
    now = 100.0
    assert is_claimable(_candidate(), now)
    assert not is_claimable(_candidate(status=CandidateStatus.USABLE), now)
    assert not is_claimable(_candidate(next_eligible_at=200.0), now)
    assert not is_claimable(_candidate(status=CandidateStatus.QUOTA, quota_reset_at=200.0), now)
    assert is_claimable(_candidate(status=CandidateStatus.QUOTA, quota_reset_at=50.0), now)
    assert not is_claimable(_candidate(transports=()), now)


def test_infra_backoff_is_bounded():
    assert infra_backoff(1, base_s=10, max_s=100) == 10
    assert infra_backoff(2, base_s=10, max_s=100) == 20
    assert infra_backoff(10, base_s=10, max_s=100) == 100


def test_deterministic_jitter_is_stable_and_bounded():
    from free.qualification.domain import deterministic_jitter

    first = deterministic_jitter("se1.nordvpn.com", 60.0)
    second = deterministic_jitter("se1.nordvpn.com", 60.0)
    assert first == second
    assert 0.0 <= first < 60.0
    assert deterministic_jitter("se1.nordvpn.com", 0.0) == 0.0
    values = {
        deterministic_jitter(f"host{i}.nordvpn.com", 60.0)
        for i in range(20)
    }
    assert len(values) > 1
