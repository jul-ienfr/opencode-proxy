"""Fault-injection suite for the qualification control plane.

Each fault is mapped to the spec taxonomy: infrastructure failures never kill a
hostname, 429 is quota, AUTH is station-level, and an unavailable source keeps
the previous queue readable.
"""

from __future__ import annotations

import sqlite3

import pytest

from free.qualification.diagnostics import classify_diagnostic
from free.qualification.discovery import DiscoveryService
from free.qualification.domain import (
    AttemptOutcome,
    AttemptStatus,
    Candidate,
    CandidateStatus,
    classify_outcome,
    next_candidate_status,
)
from free.qualification.source import NordVpnCatalogSource
from free.qualification.store import QualificationStore


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "country": "Sweden",
        "transports": ("wireguard", "openvpn-udp"),
        "potential_score": 80.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_dns_failure_is_infra_never_dead():
    outcome = AttemptOutcome(status=0, error="temporary failure in name resolution")
    status = classify_outcome(outcome)
    assert status is AttemptStatus.NO_EGRESS
    assert classify_diagnostic(str(status), outcome.error) == "dns"
    assert (
        next_candidate_status(
            CandidateStatus.PENDING,
            status,
            now=100.0,
            infra_failures=1,
        )
        is CandidateStatus.PENDING
    )
    assert (
        next_candidate_status(
            CandidateStatus.PENDING,
            status,
            now=100.0,
            infra_failures=3,
        )
        is CandidateStatus.SKIPPED
    )


def test_429_is_quota_and_never_dead():
    status = classify_outcome(AttemptOutcome(status=429))
    assert status is AttemptStatus.QUOTA
    assert classify_diagnostic(str(status), "") == "quota"
    assert (
        next_candidate_status(CandidateStatus.PENDING, status, now=100.0)
        is CandidateStatus.QUOTA
    )


def test_auth_failure_is_station_level():
    status = classify_outcome(AttemptOutcome(status=401, error="AUTH_FAILED"))
    assert status is AttemptStatus.AUTH
    assert classify_diagnostic(str(status), "AUTH_FAILED") == "auth"
    assert (
        next_candidate_status(CandidateStatus.PENDING, status, now=100.0)
        is CandidateStatus.PENDING
    )


def test_tunnel_down_is_probe_not_host_death():
    outcome = AttemptOutcome(status=0, error="tunnel_down: probe returned 000")
    status = classify_outcome(outcome)
    assert status is AttemptStatus.NO_EGRESS
    assert classify_diagnostic(str(status), outcome.error) == "probe"
    assert (
        next_candidate_status(CandidateStatus.PENDING, status, now=100.0)
        is not CandidateStatus.DEAD
    )


def test_disk_full_keeps_previous_queue_readable(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("se1.nordvpn.com")])
        current_pages = int(
            store._conn.execute("PRAGMA page_count").fetchone()[0]
        )
        store._conn.execute("PRAGMA journal_mode=DELETE")
        store._conn.execute(f"PRAGMA max_page_count={current_pages}")
        huge = _candidate(
            "too-big.nordvpn.com",
            meta={"blob": "x" * 4_000_000},
        )
        with pytest.raises(sqlite3.OperationalError):
            store.upsert_candidates([huge])
        store._conn.execute("PRAGMA max_page_count=100000")
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert store.get("too-big.nordvpn.com") is None
    finally:
        store.close()


def test_unavailable_source_keeps_previous_queue(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("se1.nordvpn.com")], now=100.0)
        source = NordVpnCatalogSource(tmp_path / "missing-api.json")
        discovery = DiscoveryService(store, source, clock=lambda: 100.0)
        report = discovery.run(now=100.0, force=True)
        assert report.status == "failed"
        assert "FileNotFoundError" in report.error
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert store.summary()["total"] == 1
    finally:
        store.close()
