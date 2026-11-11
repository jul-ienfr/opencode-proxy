"""Store tests: ordering, claims, attempt accounting, promotion."""

from __future__ import annotations

import shutil
import sqlite3

from free.qualification.domain import Attempt, AttemptStatus, Candidate, CandidateStatus
from free.qualification.store import QualificationStore


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "transports": ("wireguard", "openvpn-udp"),
        "potential_score": 50.0,
        "quota_remaining": 400,
        "rtt_ms": 30.0,
        "load": 10.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_claim_order_is_potential_then_quota_then_rtt(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates(
            [
                _candidate("low", potential_score=10.0),
                _candidate("high", potential_score=90.0),
                _candidate("high-more-quota", potential_score=90.0, quota_remaining=500),
            ]
        )
        first = store.claim_next(1, now=100.0)
        second = store.claim_next(1, now=100.0)
        assert first is not None and first.hostname == "high-more-quota"
        assert second is not None and second.hostname == "high"
    finally:
        store.close()


def test_claim_skips_future_quota_reset_and_marks_claimed(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates(
            [
                _candidate("quota", status=CandidateStatus.QUOTA, quota_reset_at=500.0),
                _candidate("ready", status=CandidateStatus.PENDING),
            ]
        )
        claimed = store.claim_next(1, now=100.0)
        assert claimed is not None and claimed.hostname == "ready"
        again = store.claim_next(1, now=100.0)
        assert again is None
    finally:
        store.close()


def test_expired_claim_is_reclaimed_after_crash(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("se1")])
        first = store.claim_next(1, now=100.0, lease_s=10.0)
        assert first is not None
        assert store.claim_next(1, now=105.0, lease_s=10.0) is None
        reclaimed = store.claim_next(1, now=200.0, lease_s=10.0)
        assert reclaimed is not None
        assert reclaimed.hostname == "se1"
        assert reclaimed.status is CandidateStatus.CLAIMED
    finally:
        store.close()


def test_apply_attempt_quota_and_summary(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("quota")])
        attempt = Attempt(
            hostname="quota",
            station=1,
            transport="wireguard",
            status=AttemptStatus.QUOTA,
            reset_at=500.0,
            started_at=100.0,
            finished_at=100.0,
        )
        store.record_attempt(attempt)
        store.apply_attempt(
            "quota",
            attempt,
            candidate_status=CandidateStatus.QUOTA,
            next_eligible_at=500.0,
            quota_reset_at=500.0,
            now=100.0,
        )
        candidate = store.get("quota")
        assert candidate is not None
        assert candidate.status is CandidateStatus.QUOTA
        assert candidate.quota_reset_at == 500.0
        assert candidate.fail_count == 1
        summary = store.summary()
        assert summary["quota"] == 1
        assert summary["tested"] == 1
    finally:
        store.close()


def test_promote_marks_usable_and_measured(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("ok")])
        attempt = Attempt(
            hostname="ok",
            station=1,
            transport="wireguard",
            status=AttemptStatus.OK,
            egress_ip="1.2.3.4",
            latency_ms=100.0,
            started_at=100.0,
            finished_at=100.0,
        )
        store.record_attempt(attempt)
        store.apply_attempt(
            "ok",
            attempt,
            candidate_status=CandidateStatus.USABLE,
            now=100.0,
        )
        store.promote("ok", egress_ip="1.2.3.4", now=100.0)
        candidate = store.get("ok")
        assert candidate is not None
        assert candidate.status is CandidateStatus.USABLE
        assert candidate.last_egress_ip == "1.2.3.4"
        assert candidate.measured_score > 0
    finally:
        store.close()


def test_operator_pause_and_resume(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("se1")])
        assert store.set_paused("se1", paused=True) is True
        candidate = store.get("se1")
        assert candidate is not None
        assert candidate.status is CandidateStatus.SKIPPED
        assert candidate.skipped_reason == "operator-paused"
        assert store.claim_next(1, now=100.0) is None
        assert store.set_paused("se1", paused=False) is True
        resumed = store.get("se1")
        assert resumed is not None
        assert resumed.status is CandidateStatus.PENDING
        assert resumed.skipped_reason == ""
    finally:
        store.close()


def test_duplicate_attempt_in_same_round_is_rejected(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("se1")])
        first = Attempt(
            hostname="se1",
            station=1,
            transport="wireguard",
            round=0,
            status=AttemptStatus.TIMEOUT,
            epoch_id="epoch-1",
            started_at=100.0,
            finished_at=100.0,
        )
        assert store.record_attempt(first)
        assert store.record_attempt(first) == ""
        next_round = Attempt(
            hostname="se1",
            station=1,
            transport="wireguard",
            round=1,
            status=AttemptStatus.OK,
            epoch_id="epoch-1",
            started_at=200.0,
            finished_at=200.0,
        )
        assert store.record_attempt(next_round)
    finally:
        store.close()


def test_retention_purge_and_wal_checkpoint(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", retention_days=1.0)
    try:
        store.upsert_candidates([_candidate("se1")])
        old = Attempt(
            hostname="se1",
            station=1,
            transport="wireguard",
            status=AttemptStatus.OK,
            started_at=100.0,
            finished_at=100.0,
        )
        fresh = Attempt(
            hostname="se1",
            station=1,
            transport="wireguard",
            round=1,
            status=AttemptStatus.OK,
            started_at=300_000.0,
            finished_at=300_000.0,
        )
        store.record_attempt(old)
        store.record_attempt(fresh)
        assert store.purge_attempts(now=300_000.0) == 1
        store.checkpoint()
        assert store._conn.execute(
            "SELECT COUNT(*) FROM qualification_attempts"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_vacuum_backup_and_restore_are_integrity_checked(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("se1")])
        backup = tmp_path / "registry.backup.db"
        store._conn.execute("VACUUM INTO ?", (str(backup),))
    finally:
        store.close()

    check = sqlite3.connect(backup)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute(
            "SELECT COUNT(*) FROM qualification_candidates"
        ).fetchone()[0] == 1
    finally:
        check.close()

    restored_path = tmp_path / "restored.db"
    shutil.copy2(backup, restored_path)
    restored = QualificationStore(restored_path)
    try:
        assert restored.get("se1") is not None
        assert restored.summary()["total"] == 1
    finally:
        restored.close()


def test_upsert_preserves_state_but_updates_catalog(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("se1", load=10.0)])
        store.claim_next(1, now=100.0)
        store.upsert_candidates([_candidate("se1", load=20.0, country="Sweden")])
        candidate = store.get("se1")
        assert candidate is not None
        assert candidate.status is CandidateStatus.CLAIMED
        assert candidate.load == 20.0
        assert candidate.country == "Sweden"
    finally:
        store.close()

def test_close_epoch_persists_per_run_counters(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.create_epoch("e1", total=10, now=100.0)
        store.close_epoch(
            "e1",
            status="partial",
            now=110.0,
            tested=4,
            usable=2,
            quota=1,
            infra=1,
            dead=0,
            skipped=0,
        )
        row = store._conn.execute(
            "SELECT status, tested, usable, quota, infra, dead, skipped"
            " FROM qualification_epochs WHERE epoch_id='e1'"
        ).fetchone()
        assert tuple(row) == ("partial", 4, 2, 1, 1, 0, 0)
    finally:
        store.close()


def test_close_running_epochs_is_namespaced_and_keeps_the_live_one(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.create_epoch("qualifier-7-1", now=100.0)
        store.create_epoch("qualifier-7-2", now=101.0)
        store.create_epoch("qualifier-8-1", now=102.0)
        store.create_epoch("epoch-manual", now=103.0)
        closed = store.close_running_epochs(
            "qualifier-7-", now=200.0, except_id="qualifier-7-2"
        )
        assert closed == 1
        statuses = dict(
            store._conn.execute(
                "SELECT epoch_id, status FROM qualification_epochs"
            )
        )
        assert statuses["qualifier-7-1"] == "partial"
        assert statuses["qualifier-7-2"] == "running"
        assert statuses["qualifier-8-1"] == "running"
        assert statuses["epoch-manual"] == "running"
    finally:
        store.close()
