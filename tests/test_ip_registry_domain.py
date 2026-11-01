"""Domain/state-machine tests for the IP registry (Phase 0)."""

from __future__ import annotations

from free.ip_registry.domain import (
    IpRecord,
    IpState,
    Lease,
    RuntimeResult,
    can_transition,
)


def _record(**kwargs) -> IpRecord:
    return IpRecord(
        egress_ip="87.120.102.53",
        hostname="al109.nordvpn.com",
        country="Albania",
        **kwargs,
    )


def test_allowed_transitions():
    assert can_transition(IpState.FRESH, IpState.CLAIMED, now=100.0)
    assert can_transition(IpState.CANDIDATE, IpState.EXHAUSTED, now=100.0)
    assert can_transition(IpState.CLAIMED, IpState.ASSIGNED, now=100.0)
    assert can_transition(IpState.ASSIGNED, IpState.CANDIDATE, now=100.0)
    assert can_transition(IpState.ASSIGNED, IpState.EXHAUSTED, now=100.0)


def test_forbidden_transitions():
    assert not can_transition(IpState.RETIRED, IpState.CANDIDATE, now=100.0)
    assert not can_transition(IpState.FRESH, IpState.ASSIGNED, now=100.0)
    assert not can_transition(IpState.EXHAUSTED, IpState.ASSIGNED, now=100.0)


def test_exhausted_returns_only_after_reset():
    record = _record(state=IpState.EXHAUSTED, reset_at=1000.0)
    assert not can_transition(IpState.EXHAUSTED, IpState.CANDIDATE, now=999.0, record=record)
    assert can_transition(IpState.EXHAUSTED, IpState.CANDIDATE, now=1000.0, record=record)


def test_error_returns_only_after_cooldown():
    record = _record(state=IpState.ERROR, error_until=500.0)
    assert not can_transition(IpState.ERROR, IpState.CANDIDATE, now=499.0, record=record)
    assert can_transition(IpState.ERROR, IpState.CANDIDATE, now=500.0, record=record)


def test_lease_validity():
    lease = Lease(
        lease_id="l1",
        egress_ip="87.120.102.53",
        hostname="al109.nordvpn.com",
        station=6,
        expires_at=100.0,
    )
    assert lease.is_valid(99.9)
    assert not lease.is_valid(100.0)


def test_record_guards():
    now = 100.0
    assert _record(lease_id="l1", lease_expires_at=now + 1).is_leased(now)
    assert not _record(lease_id="l1", lease_expires_at=now - 1).is_leased(now)
    assert _record(reset_at=now + 1).is_exhausted(now)
    assert not _record(reset_at=now - 1).is_exhausted(now)
    assert _record(error_until=now + 1).is_in_error_cooldown(now)
    assert _record(assigned_station=6).is_assigned()


def test_with_state_increments_version():
    record = _record()
    updated = record.with_state(IpState.CANDIDATE)
    assert updated.state is IpState.CANDIDATE
    assert updated.version == record.version + 1


def test_runtime_result_quota_flag():
    result = RuntimeResult(
        request_id="req1",
        station=6,
        hostname="al109.nordvpn.com",
        egress_ip="87.120.102.53",
        observed_at=100.0,
        status=429,
        retry_after_s=3600,
        error_type="FreeUsageLimitError",
        content_started=True,
    )
    assert result.is_quota_429
    assert not RuntimeResult(
        request_id="req2",
        station=6,
        hostname="al109.nordvpn.com",
        egress_ip="87.120.102.53",
        observed_at=100.0,
        status=429,
    ).is_quota_429
