"""SQLite registry behaviour tests (claim/record/assign/release/runtime)."""

from __future__ import annotations

import pytest

from free.ip_registry.domain import (
    GeoFilter,
    IpRecord,
    IpState,
    ProbeResult,
    RuntimeResult,
)
from free.ip_registry.sqlite_registry import SqliteIpRegistry


@pytest.fixture
def registry(tmp_path):
    reg = SqliteIpRegistry(str(tmp_path / "registry.db"))
    yield reg
    reg.close()


def _record(egress: str = "87.120.102.53", **kwargs) -> IpRecord:
    return IpRecord(
        egress_ip=egress,
        hostname="al110.nordvpn.com",
        country=kwargs.pop("country", "Albania"),
        **kwargs,
    )


def _seed(reg: SqliteIpRegistry, **kwargs) -> IpRecord:
    return reg.upsert_record(_record(**kwargs), now=kwargs.pop("now", 1000.0))


def test_eligible_lists_fresh(registry):
    _seed(registry)
    assert [r.egress_ip for r in registry.eligible(now=1000.0)] == ["87.120.102.53"]


def test_claim_record_assign_release(registry):
    _seed(registry)
    lease = registry.claim("87.120.102.53", station=6, ttl_s=300, now=1000.0)
    assert lease is not None
    assert registry.get("87.120.102.53").state is IpState.CLAIMED

    registry.record(lease, ProbeResult(status=200, tested_at=1001.0, latency_ms=120.0))
    assigned = registry.assign(lease, now=1002.0)
    assert assigned is not None
    assert assigned.state is IpState.ASSIGNED
    assert assigned.assigned_station == 6
    assert registry.claim("87.120.102.53", station=6, ttl_s=300, now=1003.0) is None

    registry.release(lease, "rotation", now=1004.0)
    released = registry.get("87.120.102.53")
    assert released.state is IpState.CANDIDATE
    assert released.assigned_station is None


def test_429_sets_reset_and_excludes_until_reset(registry):
    _seed(registry)
    lease = registry.claim("87.120.102.53", station=6, ttl_s=300, now=1000.0)
    registry.record(
        lease,
        ProbeResult(status=429, tested_at=1000.0, retry_after_s=3600.0),
    )
    record = registry.get("87.120.102.53")
    assert record.state is IpState.EXHAUSTED
    assert record.reset_at == 4600.0
    assert registry.eligible(now=2000.0) == []
    assert [r.egress_ip for r in registry.eligible(now=4600.0)] == ["87.120.102.53"]
    registry.release(lease, "runtime_429", now=1001.0)


def test_runtime_429_records_observation_and_releases(registry):
    _seed(registry)
    lease = registry.claim("87.120.102.53", station=6, ttl_s=300, now=1000.0)
    registry.record(lease, ProbeResult(status=200, tested_at=1001.0))
    registry.assign(lease, now=1002.0)

    registry.record_runtime(
        RuntimeResult(
            request_id="req-1",
            station=6,
            hostname="al110.nordvpn.com",
            egress_ip="87.120.102.53",
            observed_at=1003.0,
            status=429,
            retry_after_s=3600.0,
            error_type="FreeUsageLimitError",
            content_started=True,
        )
    )
    record = registry.get("87.120.102.53")
    assert record.state is IpState.EXHAUSTED
    assert record.reset_at == 4603.0
    assert record.assigned_station is None

    rows = registry._conn.execute(
        "SELECT released_at, reason, result FROM assignments WHERE egress_ip=?",
        ("87.120.102.53",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["released_at"] == 1003.0
    assert rows[0]["reason"] == "runtime_429"
    assert rows[0]["result"] == "429"

    obs = registry._conn.execute(
        "SELECT status, reset_at, attempt_id FROM observations WHERE egress_ip=?",
        ("87.120.102.53",),
    ).fetchall()
    assert any(r["status"] == 429 and r["attempt_id"] == "req-1" for r in obs)


def test_runtime_429_idempotent_keeps_max_reset(registry):
    _seed(registry)
    lease = registry.claim("87.120.102.53", station=6, ttl_s=300, now=1000.0)
    registry.record(lease, ProbeResult(status=200, tested_at=1001.0))
    registry.assign(lease, now=1002.0)

    for request_id, retry_after in (("r1", 1000.0), ("r2", 5000.0)):
        registry.record_runtime(
            RuntimeResult(
                request_id=request_id,
                station=6,
                hostname="al110.nordvpn.com",
                egress_ip="87.120.102.53",
                observed_at=1003.0,
                status=429,
                retry_after_s=retry_after,
                error_type="FreeUsageLimitError",
            )
        )
    record = registry.get("87.120.102.53")
    assert record.reset_at == 6003.0
    open_assignments = registry._conn.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE egress_ip=? AND released_at IS NULL",
        ("87.120.102.53",),
    ).fetchone()["n"]
    assert open_assignments == 0


def test_reconcile_releases_expired_lease(registry):
    _seed(registry)
    registry.claim("87.120.102.53", station=6, ttl_s=10, now=1000.0)
    result = registry.reconcile(now=1020.0)
    assert result["released_leases"] == 1
    record = registry.get("87.120.102.53")
    assert record.state is IpState.CANDIDATE
    assert record.lease_id is None


def test_geo_filter(registry):
    _seed(registry, country="Albania")
    _seed(registry, egress="1.2.3.4", country="France")
    geo = GeoFilter(countries=frozenset({"France"}), require=True)
    assert [r.egress_ip for r in registry.eligible(geo=geo, now=1000.0)] == ["1.2.3.4"]
