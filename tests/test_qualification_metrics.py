"""Metrics projection tests for the qualification control plane."""

from __future__ import annotations

from free.qualification.domain import Attempt, AttemptStatus, Candidate, CandidateStatus
from free.qualification.metrics import render_prometheus, snapshot
from free.qualification.store import QualificationStore


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "country": "Sweden",
        "transports": ("wireguard",),
        "potential_score": 80.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_snapshot_and_prometheus_rendering(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("a"), _candidate("b")])
        store.upsert_server_transports([_candidate("a")])
        attempt = Attempt(
            hostname="a",
            station=1,
            transport="wireguard",
            status=AttemptStatus.OK,
            egress_ip="1.2.3.4",
            rtt_ms=30.0,
            started_at=100.0,
            finished_at=100.0,
        )
        store.record_attempt(attempt)
        store.apply_attempt(
            "a",
            attempt,
            candidate_status=CandidateStatus.USABLE,
            now=100.0,
        )
        store.promote("a", egress_ip="1.2.3.4", now=100.0)
    finally:
        store.close()

    values = snapshot(db)
    assert values["qualification_total"] == 2
    assert values["qualification_tested"] == 1
    assert values["qualification_usable"] == 1
    assert values["qualification_coverage_ratio"] == 0.5
    assert values["qualification_transports_healthy"] == 1
    assert values["qualification_transports_degraded"] == 0

    text = render_prometheus(values)
    assert "# TYPE qualification_tested gauge" in text
    assert "qualification_tested 1" in text
    assert "qualification_coverage_ratio 0.5" in text


def test_missing_database_renders_zeroes(tmp_path):
    values = snapshot(tmp_path / "missing.db")
    assert values["qualification_total"] == 0
    assert values["qualification_coverage_ratio"] == 0.0
    assert "qualification_usable 0" in render_prometheus(values)
