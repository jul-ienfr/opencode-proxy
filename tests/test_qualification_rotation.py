"""Rotation planner tests: headroom, standby, last station, unique IP."""

from __future__ import annotations

from free.qualification.domain import Candidate
from free.qualification.rotation import (
    KEEP,
    PROMOTE_STANDBY,
    PROTECT_LAST,
    ROTATE,
    WAIT,
    RotationPlanner,
    StationSnapshot,
)
from free.qualification.store import QualificationStore


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "transports": ("wireguard",),
        "potential_score": 70.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_headroom_ok_keeps_station(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        planner = RotationPlanner(store)
        decision = planner.plan(StationSnapshot(1, current_ip="1.1.1.1", quota_remaining=400))
        assert decision.action == KEEP
    finally:
        store.close()


def test_low_headroom_with_standby_promotes(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        planner = RotationPlanner(store)
        decision = planner.plan(
            StationSnapshot(1, current_ip="1.1.1.1", quota_remaining=50, standby_ip="2.2.2.2")
        )
        assert decision.action == PROMOTE_STANDBY
    finally:
        store.close()


def test_low_headroom_without_standby_rotates(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("next.nordvpn.com")])
        planner = RotationPlanner(store)
        decision = planner.plan(StationSnapshot(1, current_ip="1.1.1.1", quota_remaining=10), usable_stations=6)
        assert decision.action == ROTATE
        assert decision.candidate is not None
        assert decision.candidate.hostname == "next.nordvpn.com"
    finally:
        store.close()


def test_last_station_is_protected_without_standby(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("next.nordvpn.com")])
        planner = RotationPlanner(store, last_station_protection=True)
        decision = planner.plan(StationSnapshot(1, current_ip="1.1.1.1", quota_remaining=0), usable_stations=1)
        assert decision.action == PROTECT_LAST
    finally:
        store.close()


def test_paused_station_waits(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        planner = RotationPlanner(store)
        assert planner.plan(StationSnapshot(1, state="paused", current_ip="1.1.1.1")).action == WAIT
        assert planner.plan(StationSnapshot(1, state="repairing", current_ip="1.1.1.1")).action == WAIT
    finally:
        store.close()


def test_no_current_ip_rotates_or_waits(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        planner = RotationPlanner(store)
        assert planner.plan(StationSnapshot(1)).action == WAIT
        store.upsert_candidates([_candidate("next.nordvpn.com")])
        assert planner.plan(StationSnapshot(1)).action == ROTATE
    finally:
        store.close()


def test_candidate_selection_skips_ip_used_by_another_station(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates(
            [
                _candidate("used.nordvpn.com", potential_score=90.0, last_egress_ip="9.9.9.9"),
                _candidate("free.nordvpn.com", potential_score=80.0, last_egress_ip="8.8.8.8"),
            ]
        )
        store._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS egress_options (
                key TEXT PRIMARY KEY, provider TEXT, kind TEXT, host TEXT,
                port INTEGER, station TEXT, egress_ip TEXT, status TEXT
            )
            """
        )
        store._conn.execute(
            "INSERT INTO egress_options(key, provider, kind, host, port, station, egress_ip, status) "
            "VALUES('k','nordvpn','nordvpn_station','other',0,'2','9.9.9.9','usable')"
        )
        store._conn.commit()
        planner = RotationPlanner(store)
        candidate = planner.select_candidate(station=1)
        assert candidate is not None
        assert candidate.hostname == "free.nordvpn.com"
    finally:
        store.close()


def test_zero_standby_size_ignores_standby(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("next")])
        planner = RotationPlanner(store)
        decision = planner.plan(
            StationSnapshot(
                station=1,
                current_ip="1.2.3.4",
                quota_remaining=10,
                quota_estimate=500,
                standby_ip="5.6.7.8",
                standby_size=0,
            ),
            usable_stations=2,
        )
        assert decision.action == "rotate"
        assert decision.reason == "headroom-low"
    finally:
        store.close()


def test_headroom_percentage():
    assert StationSnapshot(1, quota_remaining=400, quota_estimate=500).headroom_pct == 80.0
    assert StationSnapshot(1, quota_remaining=None).headroom_pct == 100.0
    assert StationSnapshot(1, quota_remaining=0).headroom_pct == 0.0
