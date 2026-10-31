"""Contrats de la couche fiabilité stations : santé, quota, sélection, réparation.

Tests purs, sans Docker/réseau : ils verrouillent les invariants du plan
(2 probes, réserve/quota dynamique, sélection lexicographique, rotation une
station à la fois, escalade auth bornée).
"""

from __future__ import annotations

from types import SimpleNamespace

from free.best_ip import BestIpSelector
from free.ip_quota import BAD, DRAINING, EXHAUSTED, IpQuotaRegistry, QuotaProfile
from free.repair import (
    AUTH_GLOBAL,
    AUTH_THROTTLE,
    CREDENTIALS,
    PARK,
    RECREATE,
    RESTART,
    STOP,
    GlobalAuthGuard,
    RepairPolicy,
    RepairState,
    classify_repair_cause,
)
from free.rotation_planner import RotationPlanner
from free.station_health import (
    PARKED,
    PROBING,
    REPAIRING,
    SUSPECT,
    USABLE,
    StationHealth,
    station_contract,
)


def _station(sid: int = 1, ip: str = "10.0.0.1", *, auth_failed: bool = False, egress: str = "tunnel_up_full"):
    return SimpleNamespace(
        _station=sid,
        current_ip=ip,
        pid=None,
        _quota_per_ip=150,
        status="connected",
        _auth_failed=auth_failed,
        _egress_state=egress,
        _last_effective_ok_at=0.0,
    )


def test_station_health_needs_two_bad_probes_then_two_good_probes():
    health = StationHealth(station=1)
    assert health.state == USABLE
    assert health.observe_probe(False, now=100.0) == SUSPECT
    assert health.observe_probe(True, now=101.0) == SUSPECT
    assert health.observe_probe(True, now=102.0) == USABLE
    assert health.observe_probe(False, now=103.0) == SUSPECT
    assert health.observe_probe(False, now=104.0) == REPAIRING
    # Une action réussie repasse par probing, jamais usable direct.
    health.repair_succeeded(now=105.0)
    assert health.state == PROBING
    assert health.observe_probe(True, now=106.0) == PROBING
    assert health.observe_probe(True, now=107.0) == USABLE


def test_station_health_repairing_can_recover_from_external_probes():
    health = StationHealth(station=2)
    assert health.observe_probe(False, now=100.0) == SUSPECT
    assert health.observe_probe(False, now=101.0) == REPAIRING
    # Réparation externe (watchdog/stack) : deux vrais bons probes rouvrent.
    assert health.observe_probe(True, now=102.0) == PROBING
    assert health.observe_probe(True, now=103.0) == USABLE
    assert health.action_required is False


def test_egress_park_is_cleared_by_egress_proof_but_auth_park_is_not():
    health = StationHealth(station=4)
    health.park(now=100.0, seconds=1800.0, cause="egress recreate failed")
    assert health.clear_egress_park(now=200.0) is True
    assert health.state == PROBING
    health.park(now=300.0, seconds=1800.0, cause="auth_throttle")
    assert health.clear_egress_park(now=400.0) is False
    assert health.state == PARKED


def test_station_health_park_is_excluded_and_resumes_after_deadline():
    health = StationHealth(station=4)
    health.park(now=100.0, seconds=1800.0, cause="auth_throttle")
    assert health.state == PARKED
    assert health.usable is False
    assert health.resume_if_due(now=1000.0) is False
    assert health.resume_if_due(now=1901.0) is True
    assert health.state == REPAIRING


def test_station_contract_rejects_auth_egress_and_stale():
    ok, reason = station_contract(_station(), now=100.0)
    assert ok is True and reason is None
    ok, reason = station_contract(_station(auth_failed=True), now=100.0)
    assert ok is False and reason == "auth-failed"
    ok, reason = station_contract(_station(egress="tunnel_down"), now=100.0)
    assert ok is False and reason == "egress=tunnel_down"
    st = _station()
    st._last_effective_ok_at = 10.0
    ok, reason = station_contract(st, now=100.0)
    assert ok is False and reason == "egress-stale"


def test_quota_profile_uses_percentages_not_hardcoded_150():
    profile = QuotaProfile.from_config(
        {
            "quota_per_ip": 500,
            "quota_per_ip_by_model": {"deepseek-v4.1-flash-free": 150, "glm-5.3-flash-free": 8},
            "ip_quota": {"quota_reserve_pct": 10, "rotation_threshold_pct": 20},
        }
    )
    assert profile.profile_for("deepseek-v4.1-flash-free") == 150
    assert profile.reserve_for("deepseek-v4.1-flash-free") == 15
    assert profile.rotation_headroom_for("deepseek-v4.1-flash-free") == 30
    assert profile.rotation_at_used("deepseek-v4.1-flash-free") == 120
    assert profile.reserve_for("glm-5.3-flash-free") == 1
    assert profile.rotation_headroom_for("glm-5.3-flash-free") == 2
    assert profile.rotation_at_used("glm-5.3-flash-free") == 6


def test_lease_bad_429_without_success_is_blacklisted_then_recovers_after_ttl():
    profile = QuotaProfile.from_config(
        {
            "quota_per_ip_by_model": {"m": 20},
            "ip_quota": {"quota_reserve_pct": 10, "rotation_threshold_pct": 20},
        }
    )
    reg = IpQuotaRegistry(profile)
    st = _station(ip="1.1.1.1")
    reg.note_request("m", st, now=10.0)
    assert reg.note_429("m", st, now=11.0) == BAD
    ok, reason = reg.model_usable("m", st, now=12.0)
    assert ok is False and reason == "bad-ip"
    # TTL expiré : redevient qualifiée, mais pas active sans preuve.
    ok, reason = reg.model_usable("m", st, now=11.0 + profile.bad_ip_ttl_s + 1.0)
    assert ok is True and reason is None


def test_lease_429_after_success_is_quota_not_bad_ip():
    profile = QuotaProfile.from_config(
        {
            "quota_per_ip_by_model": {"m": 20},
            "ip_quota": {"quota_reserve_pct": 10, "rotation_threshold_pct": 20},
        }
    )
    reg = IpQuotaRegistry(profile)
    st = _station(ip="2.2.2.2")
    for i in range(17):
        reg.note_request("m", st, now=10.0 + i)
    reg.note_success("m", st, now=30.0)
    assert reg.note_429("m", st, now=31.0) in (DRAINING, EXHAUSTED)
    lease = reg.lease_for("m", st, now=31.0)
    assert lease is not None and lease.state != BAD
    ok, reason = reg.model_usable("m", st, now=31.0)
    assert ok is False
    assert reason in ("quota-blocked", "draining", "reserve", "exhausted")


def test_selector_prefers_headroom_over_raw_latency():
    profile = QuotaProfile.from_config(
        {
            "quota_per_ip_by_model": {"m": 150},
            "ip_quota": {"quota_reserve_pct": 10, "rotation_threshold_pct": 20},
        }
    )
    quota = IpQuotaRegistry(profile)
    fast_low = _station(1, "3.3.3.3")
    slow_high = _station(2, "4.4.4.4")
    # IP 1 : rapide mais presque au mur ; IP 2 : plus lente mais gros quota.
    for i in range(125):
        quota.note_request("m", fast_low, now=10.0 + i)
    for i in range(10):
        quota.note_request("m", slow_high, now=10.0 + i)
    quota.note_success("m", fast_low, now=200.0)
    quota.note_success("m", slow_high, now=201.0)
    selector = BestIpSelector(
        quota,
        latency_provider=lambda st: 500.0 if st._station == 1 else 3000.0,
    )
    chosen = selector.choose([fast_low, slow_high], "m", now=210.0)
    assert chosen is slow_high


def test_selector_excludes_draining_and_bad():
    profile = QuotaProfile.from_config({"quota_per_ip_by_model": {"m": 20}})
    quota = IpQuotaRegistry(profile)
    draining = _station(1, "5.5.5.5")
    bad = _station(2, "6.6.6.6")
    for i in range(17):
        quota.note_request("m", draining, now=10.0 + i)
    quota.note_success("m", draining, now=30.0)
    quota.note_result("m", bad, 429, now=31.0)
    selector = BestIpSelector(quota)
    assert selector.choose([draining, bad], "m", now=32.0) is None
    reasons = {row["reason"] for row in selector.last_rejections}
    assert "bad-ip" in reasons
    assert reasons & {"draining", "reserve", "quota-blocked", "exhausted"}


def test_shared_ip_shares_the_same_quota_lease():
    profile = QuotaProfile.from_config({"quota_per_ip_by_model": {"m": 20}})
    reg = IpQuotaRegistry(profile)
    st1 = _station(1, "same-ip")
    st2 = _station(2, "same-ip")
    reg.note_request("m", st1, now=10.0)
    reg.note_request("m", st2, now=11.0)
    lease1 = reg.lease_for("m", st1, now=12.0)
    lease2 = reg.lease_for("m", st2, now=12.0)
    assert lease1 is lease2
    assert lease1 is not None
    assert lease1.used == 2
    assert lease1.headroom == 18


def test_selector_deduplicates_shared_ip():
    profile = QuotaProfile.from_config({"quota_per_ip_by_model": {"m": 20}})
    quota = IpQuotaRegistry(profile)
    st1 = _station(1, "shared-ip")
    st2 = _station(2, "shared-ip")
    selector = BestIpSelector(quota)
    ranked = selector.rank([st1, st2], "m", now=10.0)
    assert len(ranked) == 1
    assert any(row.get("reason") == "duplicate-ip" for row in selector.last_rejections)


def test_rotation_planner_one_at_a_time_and_spare_promotion():
    profile = QuotaProfile.from_config({"quota_per_ip_by_model": {"m": 20}})
    quota = IpQuotaRegistry(profile)
    st1 = _station(1, "7.7.7.7")
    st2 = _station(2, "8.8.8.8")
    for i in range(19):
        quota.note_request("m", st1, now=10.0 + i)
        quota.note_request("m", st2, now=10.0 + i)
    quota.note_success("m", st1, now=40.0)
    quota.note_success("m", st2, now=41.0)
    leases = [quota.lease_for("m", st1, now=50.0), quota.lease_for("m", st2, now=50.0)]
    planner = RotationPlanner(profile=profile)

    # Sans spare : une seule rotation, et pas sur la dernière station active.
    decision = planner.plan(leases, active_count=2, qualified_spares=0, now=50.0)
    assert decision is not None and decision.action == "rotate_one" and decision.spare_used is False
    planner.note_started(now=50.0)
    assert planner.plan(leases, active_count=2, qualified_spares=0, now=51.0) is None
    planner.note_finished()

    # Avec spare : promotion sans trou.
    decision = planner.plan(leases, active_count=2, qualified_spares=1, now=52.0)
    assert decision is not None and decision.action == "promote_spare" and decision.spare_used is True

    # Dernière station : jamais de rotation.
    planner2 = RotationPlanner(profile=profile)
    assert planner2.plan([leases[0]], active_count=1, qualified_spares=0, now=53.0) is None


def test_repair_policy_sequence_and_park():
    policy = RepairPolicy.from_config(
        {
            "station_repair": {
                "enabled": True,
                "auth_backoff_s": [300, 900, 1800],
                "park_s": 1800,
            }
        }
    )
    state = RepairState()
    assert policy.step_for(0) == RESTART
    assert policy.step_for(1) == STOP
    assert policy.step_for(2) == STOP
    assert policy.step_for(3) == "pin_last_good"
    assert policy.step_for(4) == RECREATE
    assert policy.step_for(5) == "stack"
    assert policy.step_for(6) == PARK
    assert policy.stop_delay_for(1) == 300
    assert policy.stop_delay_for(2) == 900
    state.park(policy, now=100.0, cause="auth_throttle")
    assert state.due(now=200.0) is False
    assert state.due(now=1901.0) is True


def test_global_auth_guard_arms_one_probe_pause():
    guard = GlobalAuthGuard(window_s=60, min_stations=2, pause_s=300, max_pause_s=900)
    assert guard.note_failure("1", now=100.0) is False
    assert guard.note_failure("2", now=101.0) is True
    assert guard.remaining(now=102.0) == 299.0
    assert guard.allow_probe("1", now=102.0) is True
    assert guard.allow_probe("2", now=102.0) is False
    assert guard.allow_probe("1", now=102.0) is True


def test_repair_cause_classification_order():
    assert classify_repair_cause(credentials_suspect=True, auth_failed=True) == CREDENTIALS
    assert classify_repair_cause(auth_global=True, auth_failed=True) == AUTH_GLOBAL
    assert classify_repair_cause(auth_failed=True) == AUTH_THROTTLE
    assert classify_repair_cause(egress_dead=True) == "egress"
