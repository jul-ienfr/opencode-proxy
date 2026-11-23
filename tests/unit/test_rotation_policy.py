from free import rotation_policy as rp


def test_quota_release_requires_reset_at():
    assert not rp.should_release(rp.ReleaseEvidence(kind=rp.RELEASE_QUOTA))[0]
    ok, reason = rp.should_release(
        rp.ReleaseEvidence(kind=rp.RELEASE_QUOTA, reset_at=1000.0)
    )
    assert ok and "reset_at" in reason


def test_burn_release_requires_wire_fp():
    assert not rp.should_release(rp.ReleaseEvidence(kind=rp.RELEASE_BURN))[0]
    assert rp.should_release(
        rp.ReleaseEvidence(kind=rp.RELEASE_BURN, wire_fp="bad", reset_at=1.0)
    )[0]


def test_mismatch_requires_two_agreeing_readers():
    one = rp.ReleaseEvidence(
        kind=rp.RELEASE_MISMATCH, readers=("ipify",), readers_agree=True
    )
    two = rp.ReleaseEvidence(
        kind=rp.RELEASE_MISMATCH, readers=("ipify", "gluetun"), readers_agree=True
    )
    assert not rp.should_release(one)[0]
    assert rp.should_release(two)[0]


def test_ambiguous_evidence_never_releases():
    assert not rp.should_release(rp.ReleaseEvidence(kind="single_429"))[0]
    assert not rp.should_release(rp.ReleaseEvidence(kind="timeout"))[0]


def test_rotation_guard_interval_and_coalescing():
    now = [1000.0]
    guard = rp.RotationGuard(min_interval_s=600.0, max_per_hour=6, clock=lambda: now[0])
    assert guard.allow("nordvpn", 1)[0]
    guard.record("nordvpn", 1)
    ok, reason = guard.allow("nordvpn", 1)
    assert not ok and "interval" in reason
    assert guard.coalesced("nordvpn", 1) == 1
    now[0] += 601.0
    assert guard.allow("nordvpn", 1)[0]


def test_rotation_guard_hourly_cap_and_hard_bypass():
    now = [1000.0]
    guard = rp.RotationGuard(min_interval_s=0.0, max_per_hour=2, clock=lambda: now[0])
    for station in (1, 2):
        assert guard.allow("nordvpn", station)[0]
        guard.record("nordvpn", station)
    ok, reason = guard.allow("nordvpn", 3)
    assert not ok and "cap" in reason
    assert guard.allow("nordvpn", 3, hard=True)[0]


def test_rotation_guard_hard_bypasses_interval():
    now = [1000.0]
    guard = rp.RotationGuard(min_interval_s=600.0, max_per_hour=6, clock=lambda: now[0])
    guard.record("nordvpn", 1)
    assert not guard.allow("nordvpn", 1)[0]
    assert guard.allow("nordvpn", 1, hard=True)[0]
