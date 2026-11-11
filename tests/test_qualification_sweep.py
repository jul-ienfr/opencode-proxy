"""Sweep tests with a fake station adapter: all transports, cycle, resume."""

from __future__ import annotations

import asyncio

import pytest

from free.qualification import readmodel
from free.qualification.domain import AttemptOutcome, AttemptStatus, Candidate, CandidateStatus
from free.qualification.store import QualificationStore
from free.qualification.sweep import QualificationSweep, SweepConfig


class FakeAdapter:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    async def qualify(self, candidate: Candidate, transport: str, station: int) -> AttemptOutcome:
        self.calls.append((candidate.hostname, transport))
        if not self.script:
            return AttemptOutcome(status=0, error="script exhausted")
        return self.script.pop(0)


class BlockingAdapter:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def qualify(self, candidate: Candidate, transport: str, station: int) -> AttemptOutcome:
        self.started.set()
        await self.release.wait()
        return AttemptOutcome(status=200, egress_ip="1.2.3.4")


class BoomAdapter:
    async def qualify(self, candidate: Candidate, transport: str, station: int) -> AttemptOutcome:
        raise RuntimeError("adapter exploded")


class TopDecileAdapter:
    async def qualify(self, candidate: Candidate, transport: str, station: int) -> AttemptOutcome:
        index = int(candidate.hostname.rsplit("-", 1)[-1])
        if index < 10:
            return AttemptOutcome(
                status=200,
                valid=True,
                egress_ip=f"10.0.0.{index + 1}",
                rtt_ms=20.0 + index,
            )
        return AttemptOutcome(status=429, reset_at=500.0)


def _candidate(hostname: str = "se1.nordvpn.com", **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "transports": ("wireguard", "openvpn-udp", "openvpn-tcp"),
        "potential_score": 90.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


@pytest.mark.asyncio
async def test_wireguard_fails_then_openvpn_udp_then_tcp(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        adapter = FakeAdapter(
            [
                AttemptOutcome(status=0, error="no egress"),
                AttemptOutcome(status=429, reset_at=500.0),
                AttemptOutcome(status=200, egress_ip="1.2.3.4", latency_ms=80.0),
            ]
        )
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1))
        await sweep.qualify(_candidate())
        assert [call[1] for call in adapter.calls] == ["wireguard", "openvpn-udp", "openvpn-tcp"]
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.USABLE
        assert candidate.last_egress_ip == "1.2.3.4"
        summary = store.summary()
        assert summary["tested"] == 1
        assert summary["usable"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_all_fail_then_wireguard_round_two(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        adapter = FakeAdapter(
            [
                AttemptOutcome(status=0, error="no egress"),
                AttemptOutcome(status=0, error="no egress"),
                AttemptOutcome(status=0, error="no egress"),
                AttemptOutcome(status=200, egress_ip="5.6.7.8", latency_ms=90.0),
            ]
        )
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1, transport_rounds=2))
        await sweep.qualify(_candidate())
        first = store.get("se1.nordvpn.com")
        assert first is not None
        assert first.transport_cursor == "wireguard"
        assert first.transport_round == 1
        await sweep.qualify(first)
        assert [call[1] for call in adapter.calls] == [
            "wireguard",
            "openvpn-udp",
            "openvpn-tcp",
            "wireguard",
        ]
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.USABLE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_top_score_decile_produces_most_successes(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        candidates = [
            _candidate(
                f"host-{index:02d}",
                potential_score=float(100 - index),
                transports=("wireguard",),
            )
            for index in range(100)
        ]
        store.upsert_candidates(candidates)
        sweep = QualificationSweep(
            store,
            TopDecileAdapter(),
            config=SweepConfig(station=1, shadow=False, transport_rounds=1),
            clock=lambda: 100.0,
        )
        await sweep.run_epoch(epoch_id="correlation", max_hosts=100)
        summary = store.summary()
        assert summary["usable"] == 10
        assert summary["quota"] == 90
        for index in range(100):
            candidate = store.get(f"host-{index:02d}")
            assert candidate is not None
            expected = CandidateStatus.USABLE if index < 10 else CandidateStatus.QUOTA
            assert candidate.status is expected
    finally:
        store.close()


@pytest.mark.asyncio
async def test_attempt_records_preflight_metrics(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        candidate = _candidate(
            meta={
                "dns_ms": 5.0,
                "connect_ms": 10.0,
                "handshake_ms": 15.0,
                "endpoint_ip": "1.2.3.4",
            }
        )
        store.upsert_candidates([candidate])
        sweep = QualificationSweep(
            store,
            FakeAdapter(
                [
                    AttemptOutcome(
                        status=200,
                        valid=True,
                        egress_ip="5.6.7.8",
                        rtt_ms=20.0,
                    )
                ]
            ),
            config=SweepConfig(station=1, shadow=False),
            clock=lambda: 100.0,
        )
        await sweep.qualify(candidate)
    finally:
        store.close()
    attempt = readmodel.attempts(db)["items"][0]
    assert attempt["dns_ms"] == 5.0
    assert attempt["connect_ms"] == 10.0
    assert attempt["handshake_ms"] == 15.0
    assert attempt["endpoint_ip"] == "1.2.3.4"
    assert attempt["rtt_ms"] == 20.0


@pytest.mark.asyncio
async def test_quota_then_infra_stays_quota(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        adapter = FakeAdapter(
            [
                AttemptOutcome(status=429, reset_at=500.0),
                AttemptOutcome(status=0, error="no egress"),
            ]
        )
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1))
        await sweep.qualify(_candidate())
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.QUOTA
        assert candidate.quota_reset_at == 500.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_uses_persisted_transport_cursor(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        candidate = _candidate(transport_cursor="openvpn-udp", transport_round=0)
        store.upsert_candidates([candidate])
        adapter = FakeAdapter([AttemptOutcome(status=200, egress_ip="9.9.9.9")])
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1))
        await sweep.qualify(candidate)
        assert adapter.calls[0][1] == "openvpn-udp"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_auth_stops_without_host_death(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        adapter = FakeAdapter([AttemptOutcome(status=401)])
        sweep = QualificationSweep(
            store,
            adapter,
            config=SweepConfig(station=1, auth_backoff_s=3600.0),
            clock=lambda: 100.0,
        )
        await sweep.qualify(_candidate())
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert candidate.next_eligible_at == 3700.0
        assert len(adapter.calls) == 1
        assert store.claim_next(2, now=1000.0) is None
        reclaimed = store.claim_next(2, now=3700.0)
        assert reclaimed is not None
        assert reclaimed.hostname == "se1.nordvpn.com"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_epoch_claims_and_promotes_two_hosts(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a.nordvpn.com"), _candidate("b.nordvpn.com")])
        adapter = FakeAdapter(
            [
                AttemptOutcome(status=200, egress_ip="1.1.1.1"),
                AttemptOutcome(status=200, egress_ip="2.2.2.2"),
            ]
        )
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1))
        result = await sweep.run_epoch(epoch_id="test-epoch", max_hosts=2)
        assert result.claimed == 2
        assert result.promoted == 2
        assert store.summary()["usable"] == 2
    finally:
        store.close()


def test_classify_429_is_quota():
    from free.qualification.domain import classify_outcome

    assert classify_outcome(AttemptOutcome(status=429)) is AttemptStatus.QUOTA


@pytest.mark.asyncio
async def test_max_attempts_skips_without_binding(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate(transport_round=3)])
        adapter = FakeAdapter([AttemptOutcome(status=200, egress_ip="1.1.1.1")])
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1, max_attempts_per_host=3))
        await sweep.qualify(_candidate(transport_round=3))
        assert adapter.calls == []
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.SKIPPED
        assert candidate.skipped_reason == "max-attempts"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_duplicate_egress_ip_is_not_promoted(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate(transports=("wireguard",))])
        store._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS egress_options (
                key TEXT PRIMARY KEY,
                provider TEXT,
                kind TEXT,
                host TEXT,
                port INTEGER,
                station TEXT,
                egress_ip TEXT,
                status TEXT
            )
            """
        )
        store._conn.execute(
            "INSERT INTO egress_options(key, provider, kind, host, port, station, egress_ip, status) "
            "VALUES('k','nordvpn','nordvpn_station','other',0,'2','1.2.3.4','usable')"
        )
        store._conn.commit()
        adapter = FakeAdapter([AttemptOutcome(status=200, egress_ip="1.2.3.4")])
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1))
        await sweep.qualify(_candidate(transports=("wireguard",)))
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert candidate.last_result == "duplicate"
        assert candidate.status is not CandidateStatus.USABLE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_epoch_shadow_releases_and_does_not_bind(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        adapter = FakeAdapter([])
        sweep = QualificationSweep(store, adapter, config=SweepConfig(station=1))
        result = await sweep.run_epoch(epoch_id="shadow", max_hosts=1, shadow=True)
        assert result.claimed == 1
        assert adapter.calls == []
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_epoch_without_adapter_is_fail_soft(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        sweep = QualificationSweep(store, None, config=SweepConfig(station=1))
        result = await sweep.run_epoch(epoch_id="no-adapter", max_hosts=1)
        assert result.errors
        assert "no-station-adapter" in result.errors
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_epoch_adapter_exception_is_fail_soft(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate(transports=("wireguard",))])
        sweep = QualificationSweep(store, BoomAdapter(), config=SweepConfig(station=1))
        result = await sweep.run_epoch(epoch_id="boom", max_hosts=1)
        assert result.infra == 1
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert candidate.next_eligible_at > 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_qualify_unsupported_transport_skips(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate(transports=())])
        sweep = QualificationSweep(store, FakeAdapter([]), config=SweepConfig(station=1))
        attempt = await sweep.qualify(_candidate(transports=()))
        assert str(attempt.status) == "skipped"
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.SKIPPED
        assert candidate.skipped_reason == "unsupported-transport"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_qualify_without_adapter_classifies_infra(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate(transports=("wireguard",))])
        sweep = QualificationSweep(store, None, config=SweepConfig(station=1))
        attempt = await sweep.qualify(_candidate(transports=("wireguard",)))
        assert "no-station-adapter" in attempt.error
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_quota_reset_uses_deterministic_jitter(tmp_path):
    from free.qualification.domain import deterministic_jitter

    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate()])
        adapter = FakeAdapter([AttemptOutcome(status=429, reset_at=500.0)])
        sweep = QualificationSweep(
            store,
            adapter,
            config=SweepConfig(station=1, quota_retry_jitter_s=60.0),
        )
        await sweep.qualify(_candidate())
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.QUOTA
        expected = 500.0 + deterministic_jitter("se1.nordvpn.com", 60.0)
        assert candidate.quota_reset_at == pytest.approx(expected)
        assert 500.0 <= candidate.quota_reset_at < 560.0
    finally:
        store.close()

@pytest.mark.asyncio
async def test_cancelled_epoch_is_closed_and_the_claim_released(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates(
            [_candidate(transports=("wireguard",))]
        )
        adapter = BlockingAdapter()
        sweep = QualificationSweep(
            store, adapter, config=SweepConfig(station=1, max_hosts=1)
        )
        task = asyncio.create_task(
            sweep.run_epoch(epoch_id="cancel-me", max_hosts=1)
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = store._conn.execute(
            "SELECT status, finished_at, reason FROM qualification_epochs"
            " WHERE epoch_id='cancel-me'"
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["finished_at"] is not None
        assert row["reason"] == "cancelled"
        candidate = store.get("se1.nordvpn.com")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        lease = store._conn.execute(
            "SELECT lease_until FROM qualification_candidates"
            " WHERE hostname='se1.nordvpn.com'"
        ).fetchone()[0]
        assert lease == 0
    finally:
        store.close()
