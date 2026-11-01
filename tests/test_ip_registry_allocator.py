"""Allocator tests with a real SQLite registry and fake binder/probe."""

from __future__ import annotations

import pytest

from free.ip_registry.allocator import IpAllocator, StationNeed
from free.ip_registry.domain import (
    BindResult,
    IpRecord,
    IpState,
    NeedReason,
    ProbeResult,
)
from free.ip_registry.sqlite_registry import SqliteIpRegistry


class FakeBinder:
    def __init__(self, egress: dict[str, str], *, fail: set[str] | None = None, mismatch: bool = False):
        self._egress = egress
        self._fail = fail or set()
        self._mismatch = mismatch
        self.calls: list[tuple[int, str]] = []

    async def assign(self, station: int, hostname: str) -> BindResult:
        self.calls.append((station, hostname))
        if hostname in self._fail:
            return BindResult(False, hostname, error="bind-failed")
        egress = self._egress[hostname]
        if self._mismatch:
            egress = "9.9.9.9"
        return BindResult(True, hostname, egress_ip=egress)

    async def release(self, station: int) -> None:
        return None

    async def current(self, station: int) -> str | None:
        return None


class FakeProbe:
    def __init__(self, results: list[ProbeResult]):
        self._results = list(results)
        self.calls: list[tuple[int, str]] = []

    async def probe(self, station: int, model: str) -> ProbeResult:
        self.calls.append((station, model))
        return self._results.pop(0)


@pytest.fixture
def registry(tmp_path):
    reg = SqliteIpRegistry(str(tmp_path / "registry.db"))
    yield reg
    reg.close()


def _seed(reg: SqliteIpRegistry, egress: str, hostname: str, country: str = "Albania") -> IpRecord:
    return reg.upsert_record(
        IpRecord(egress_ip=egress, hostname=hostname, country=country),
        now=1000.0,
    )


async def test_allocate_200_assigns_station(registry):
    _seed(registry, "87.120.102.53", "a.nordvpn.com")
    binder = FakeBinder({"a.nordvpn.com": "87.120.102.53"})
    probe = FakeProbe([ProbeResult(status=200, tested_at=1001.0, latency_ms=80)])
    allocator = IpAllocator(registry, binder, probe, model="m", clock=lambda: 1000.0)

    outcome = await allocator.allocate(StationNeed(6, NeedReason.NO_IP))

    assert outcome.ok
    assert outcome.egress_ip == "87.120.102.53"
    assert outcome.hostname == "a.nordvpn.com"
    assert outcome.attempts == 1
    record = registry.get("87.120.102.53")
    assert record.state is IpState.ASSIGNED
    assert record.assigned_station == 6
    assert probe.calls == [(6, "m")]


async def test_429_then_200_rotates(registry):
    _seed(registry, "87.120.102.1", "a.nordvpn.com")
    _seed(registry, "87.120.102.2", "b.nordvpn.com")
    binder = FakeBinder({"a.nordvpn.com": "87.120.102.1", "b.nordvpn.com": "87.120.102.2"})
    probe = FakeProbe(
        [
            ProbeResult(status=429, tested_at=1001.0, retry_after_s=3600),
            ProbeResult(status=200, tested_at=1002.0),
        ]
    )
    allocator = IpAllocator(registry, binder, probe, model="m", clock=lambda: 1000.0)

    outcome = await allocator.allocate(StationNeed(6, NeedReason.QUOTA_429))

    assert outcome.ok
    assert outcome.attempts == 2
    statuses = sorted(
        (r.egress_ip, r.state.value) for r in registry.list_records(limit=10)
    )
    assert ("87.120.102.1", "exhausted") in statuses
    assert ("87.120.102.2", "assigned") in statuses


async def test_bind_failure_tries_next(registry):
    _seed(registry, "87.120.102.1", "a.nordvpn.com")
    _seed(registry, "87.120.102.2", "b.nordvpn.com")
    binder = FakeBinder(
        {"a.nordvpn.com": "87.120.102.1", "b.nordvpn.com": "87.120.102.2"},
        fail={"a.nordvpn.com"},
    )
    probe = FakeProbe([ProbeResult(status=200, tested_at=1001.0)])
    allocator = IpAllocator(registry, binder, probe, model="m", clock=lambda: 1000.0)

    outcome = await allocator.allocate(StationNeed(6, NeedReason.UNHEALTHY))

    assert outcome.ok
    assert outcome.hostname == "b.nordvpn.com"
    assert outcome.attempts == 2


async def test_egress_mismatch_rejects_candidate(registry):
    _seed(registry, "87.120.102.1", "a.nordvpn.com")
    binder = FakeBinder({"a.nordvpn.com": "87.120.102.1"}, mismatch=True)
    probe = FakeProbe([])
    allocator = IpAllocator(registry, binder, probe, model="m", clock=lambda: 1000.0)

    outcome = await allocator.allocate(StationNeed(6, NeedReason.NO_IP))

    assert not outcome.ok
    assert outcome.attempts == 1
    record = registry.get("87.120.102.1")
    assert record.state is IpState.CANDIDATE
    assert probe.calls == []


async def test_no_needs_does_nothing(registry):
    _seed(registry, "87.120.102.1", "a.nordvpn.com")
    binder = FakeBinder({})
    probe = FakeProbe([])
    allocator = IpAllocator(registry, binder, probe, model="m")

    report = await allocator.run([])

    assert report.outcomes == ()
    assert report.ok_count == 0
    assert registry.get("87.120.102.1").state is IpState.FRESH
    assert probe.calls == []


async def test_max_attempts_bounds_probes(registry):
    _seed(registry, "87.120.102.1", "a.nordvpn.com")
    _seed(registry, "87.120.102.2", "b.nordvpn.com")
    binder = FakeBinder({"a.nordvpn.com": "87.120.102.1", "b.nordvpn.com": "87.120.102.2"})
    probe = FakeProbe([ProbeResult(status=429, tested_at=1001.0, retry_after_s=3600)])
    allocator = IpAllocator(
        registry, binder, probe, model="m", max_attempts=1, clock=lambda: 1000.0
    )

    outcome = await allocator.allocate(StationNeed(6, NeedReason.NO_IP))

    assert not outcome.ok
    assert outcome.attempts == 1
    assert len(probe.calls) == 1
