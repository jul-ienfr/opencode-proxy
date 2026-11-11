"""Crash-resume tests: persisted cursor/round, lease and attempt idempotence."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from free.qualification.domain import (
    Attempt,
    AttemptStatus,
    Candidate,
    CandidateStatus,
)
from free.qualification.store import QualificationStore

ROOT = Path(__file__).resolve().parents[1]


def _candidate(hostname: str) -> Candidate:
    return Candidate(
        hostname=hostname,
        country="Sweden",
        transports=("wireguard", "openvpn-udp", "openvpn-tcp"),
        potential_score=80.0,
        first_seen_at=100.0,
        last_seen_at=100.0,
        updated_at=100.0,
    )


def test_killed_process_lease_is_recovered(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("crash.nordvpn.com")], now=100.0)
    finally:
        store.close()
    script = textwrap.dedent(
        """
        import os
        import sys
        from free.qualification.store import QualificationStore

        store = QualificationStore(sys.argv[1])
        claimed = store.claim_next(1, now=100.0, lease_s=30.0, epoch_id="crash")
        assert claimed is not None
        os._exit(9)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(db)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 9
    reopened = QualificationStore(db)
    try:
        assert reopened.claim_next(1, now=120.0, lease_s=30.0) is None
        recovered = reopened.claim_next(1, now=140.0, lease_s=30.0)
        assert recovered is not None
        assert recovered.hostname == "crash.nordvpn.com"
    finally:
        reopened.close()


def test_restart_resumes_cursor_without_double_counting(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("se1.nordvpn.com")], now=100.0)
        claimed = store.claim_next(1, now=100.0, lease_s=10.0, epoch_id="epoch-1")
        assert claimed is not None
        attempt = Attempt(
            hostname="se1.nordvpn.com",
            station=1,
            transport="wireguard",
            round=0,
            status=AttemptStatus.QUOTA,
            epoch_id="epoch-1",
            reset_at=500.0,
            started_at=100.0,
            finished_at=100.0,
        )
        assert store.record_attempt(attempt)
        store.apply_attempt(
            "se1.nordvpn.com",
            attempt,
            candidate_status=CandidateStatus.QUOTA,
            next_eligible_at=500.0,
            quota_reset_at=500.0,
            transport_cursor="openvpn-udp",
            transport_round=0,
            now=100.0,
        )
    finally:
        store.close()

    reopened = QualificationStore(db)
    try:
        assert reopened.summary()["tests"] == 1
        before_reset = reopened.claim_next(1, now=200.0, lease_s=10.0)
        assert before_reset is None
        resumed = reopened.claim_next(1, now=600.0, lease_s=10.0, epoch_id="epoch-1")
        assert resumed is not None
        assert resumed.hostname == "se1.nordvpn.com"
        assert resumed.transport_cursor == "openvpn-udp"
        assert resumed.transport_round == 0
        assert reopened.record_attempt(attempt) == ""
        assert reopened.summary()["tests"] == 1
    finally:
        reopened.close()
