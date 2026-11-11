"""Preflight service tests: RTT updates, failure is infra not death."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from free.qualification.domain import Candidate, CandidateStatus
from free.qualification.preflight import PreflightService
from free.qualification.store import QualificationStore


@dataclass
class FakeResult:
    hostname: str
    ok: bool
    dns_ms: float = 0.0
    connect_ms: float = 0.0
    handshake_ms: float = 0.0
    rtt_ms: float = 0.0
    endpoint_ip: str = ""
    error: str = ""


class FakeProber:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def preflight_many(self, candidates, transport, **kwargs):
        self.calls.append((list(candidates), transport, kwargs))
        return self.results


class SequentialProber:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def preflight_many(self, candidates, transport, **kwargs):
        self.calls.append((list(candidates), transport, kwargs))
        index = min(len(self.calls) - 1, len(self.batches) - 1)
        return self.batches[index]


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "transports": ("wireguard", "openvpn-udp"),
        "potential_score": 50.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


@pytest.mark.asyncio
async def test_preflight_updates_rtt_and_score(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("fast"), _candidate("slow")])
        prober = FakeProber(
            [
                FakeResult("fast", True, dns_ms=2, connect_ms=5, rtt_ms=20, endpoint_ip="1.1.1.1"),
                FakeResult("slow", False, error="dns:timeout"),
            ]
        )
        service = PreflightService(store, prober, timeout_s=1.0, backoff_s=300.0)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.requested == 2
        assert report.ok == 1
        assert report.failed == 1
        fast = store.get("fast")
        slow = store.get("slow")
        assert fast is not None and fast.rtt_ms == 20
        assert fast.potential_score > 50
        assert slow is not None
        assert slow.status is CandidateStatus.PENDING
        assert slow.next_eligible_at == 400.0
        assert slow.last_result.startswith("preflight:")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_skips_candidates_with_rtt(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("known", rtt_ms=10.0), _candidate("new")])
        prober = FakeProber([FakeResult("new", True, rtt_ms=30)])
        service = PreflightService(store, prober)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.requested == 1
        rows, transport, _ = prober.calls[0]
        assert rows[0]["host"] == "new"
        assert transport == "wireguard"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_uses_three_samples_and_median(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a")])
        prober = SequentialProber(
            [
                [FakeResult("a", True, dns_ms=10, connect_ms=20, rtt_ms=20)],
                [FakeResult("a", True, dns_ms=30, connect_ms=40, rtt_ms=40)],
                [
                    FakeResult(
                        "a",
                        True,
                        dns_ms=20,
                        connect_ms=30,
                        rtt_ms=30,
                        endpoint_ip="1.1.1.1",
                    )
                ],
            ]
        )
        service = PreflightService(store, prober, samples=3)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.ok == 1
        assert len(prober.calls) == 3
        candidate = store.get("a")
        assert candidate is not None
        assert candidate.rtt_ms == 30.0
        assert candidate.meta["dns_ms"] == 20.0
        assert candidate.meta["connect_ms"] == 30.0
        assert candidate.meta["endpoint_ip"] == "1.1.1.1"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_majority_failure_is_infra(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a")])
        prober = SequentialProber(
            [
                [FakeResult("a", False, error="dns:timeout")],
                [FakeResult("a", True, rtt_ms=20)],
                [FakeResult("a", False, error="dns:timeout")],
            ]
        )
        service = PreflightService(store, prober, samples=3, backoff_s=60.0)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.failed == 1
        candidate = store.get("a")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert candidate.next_eligible_at == 160.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_prober_exception_is_fail_soft(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a")])

        class Broken:
            async def preflight_many(self, *args, **kwargs):
                raise RuntimeError("boom")

        service = PreflightService(store, Broken(), backoff_s=60.0)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.failed == 1
        candidate = store.get("a")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert candidate.next_eligible_at == 160.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_records_handshake_resolution_and_rtt_fallback(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a")])
        prober = FakeProber(
            [
                FakeResult(
                    "a",
                    True,
                    dns_ms=4,
                    connect_ms=11,
                    handshake_ms=7,
                    rtt_ms=0,
                    endpoint_ip="2001:db8::1",
                )
            ]
        )
        service = PreflightService(store, prober, samples=1)
        report = await service.run(limit=10, transport="openvpn-udp", now=100.0)
        assert report.ok == 1
        rows, transport, kwargs = prober.calls[0]
        assert rows[0]["host"] == "a"
        assert transport == "openvpn-udp"
        assert kwargs["concurrency"] == 6
        assert kwargs["timeout"] == 5.0
        candidate = store.get("a")
        assert candidate is not None
        assert candidate.rtt_ms == 11.0  # rtt absent -> connect fallback
        assert candidate.meta["dns_ms"] == 4.0
        assert candidate.meta["handshake_ms"] == 7.0
        assert candidate.meta["endpoint_ip"] == "2001:db8::1"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_ignores_unknown_hostname_results(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a")])
        prober = FakeProber(
            [
                FakeResult("unknown", False, error="dns:timeout"),
                FakeResult("a", True, rtt_ms=20),
            ]
        )
        service = PreflightService(store, prober, samples=1)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.requested == 1
        assert report.ok == 1
        assert report.failed == 0
        candidate = store.get("a")
        assert candidate is not None
        assert candidate.status is CandidateStatus.PENDING
        assert candidate.rtt_ms == 20.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_preflight_error_is_grouped_by_cause(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("a"), _candidate("b")])
        prober = FakeProber(
            [
                FakeResult("a", False, error="dns:timeout"),
                FakeResult("b", False, error="dns:servfail"),
            ]
        )
        service = PreflightService(store, prober, samples=1, backoff_s=30.0)
        report = await service.run(limit=10, transport="wireguard", now=100.0)
        assert report.failed == 2
        assert report.errors == {"dns": 2}
        for hostname in ("a", "b"):
            candidate = store.get(hostname)
            assert candidate is not None
            assert candidate.next_eligible_at == 130.0
    finally:
        store.close()
