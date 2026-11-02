"""SQLite registry behaviour tests (claim/record/assign/release/runtime)."""

from __future__ import annotations

import pytest

from free.ip_registry.domain import (
    GeoFilter,
    IpRecord,
    IpState,
    ProbeResult,
    RuntimeResult,
    Server,
)
from free.ip_registry.sources import SourceSnapshot
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


def test_sync_catalog_persists_source_and_stale(registry):
    registry.sync_catalog(
        [
            Server(
                hostname="al41.nordvpn.com",
                country="Albania",
                source_id="gluetun_embedded_catalog",
                stale=True,
                catalog_fetched_at=42.0,
            )
        ],
        now=10.0,
    )
    server = registry.server("al41.nordvpn.com")
    assert server is not None
    assert server.source_id == "gluetun_embedded_catalog"
    assert server.stale is True
    assert server.catalog_fetched_at == 42.0
    assert registry.catalog_source_counts() == {"gluetun_embedded_catalog": 1}
    assert [s.hostname for s in registry.list_servers(stale=True)] == ["al41.nordvpn.com"]


def test_sync_snapshot_journal_and_authoritative_deactivation(registry):
    first = SourceSnapshot(
        source_id="nordvpn_api_servers",
        servers=(
            Server(hostname="a.nordvpn.com", country="Albania"),
            Server(hostname="b.nordvpn.com", country="Albania"),
        ),
        fetched_at=100.0,
        stale=False,
        complete=True,
        etag="v1",
    )
    registry.sync_snapshot(first, authoritative=True, now=100.0)
    assert registry.list_servers(active=True, limit=10)
    assert {s.hostname for s in registry.list_servers(active=True, limit=10)} == {
        "a.nordvpn.com",
        "b.nordvpn.com",
    }

    second = SourceSnapshot(
        source_id="nordvpn_api_servers",
        servers=(Server(hostname="a.nordvpn.com", country="Albania"),),
        fetched_at=200.0,
        stale=False,
        complete=True,
        etag="v2",
    )
    registry.sync_snapshot(second, authoritative=True, now=200.0)
    assert {s.hostname for s in registry.list_servers(active=True, limit=10)} == {
        "a.nordvpn.com"
    }
    assert {s.hostname for s in registry.list_servers(active=False, limit=10)} == {
        "b.nordvpn.com"
    }

    journal = registry.recent_catalog_snapshots()
    assert [row["etag"] for row in journal] == ["v2", "v1"]
    assert journal[0]["server_count"] == 1


def test_fallback_snapshot_never_deactivates_missing_servers(registry):
    primary = SourceSnapshot(
        source_id="nordvpn_api_servers",
        servers=(
            Server(hostname="a.nordvpn.com", country="Albania"),
            Server(hostname="b.nordvpn.com", country="Albania"),
        ),
        fetched_at=100.0,
        stale=False,
        complete=True,
    )
    registry.sync_snapshot(primary, authoritative=True, now=100.0)

    fallback = SourceSnapshot(
        source_id="gluetun_embedded_catalog",
        servers=(Server(hostname="a.nordvpn.com", country="Albania"),),
        fetched_at=200.0,
        stale=True,
        complete=False,
    )
    registry.sync_snapshot(fallback, authoritative=True, now=200.0)
    active = {s.hostname for s in registry.list_servers(active=True, limit=10)}
    assert active == {"a.nordvpn.com", "b.nordvpn.com"}
    assert registry.server("a.nordvpn.com").source_id == "gluetun_embedded_catalog"
    assert registry.server("a.nordvpn.com").stale is True


def test_from_config_applies_documented_scoring(monkeypatch, tmp_path):
    reg = SqliteIpRegistry.from_config(
        {
            "db_path": str(tmp_path / "cfg.db"),
            "score": {
                "availability_weight": 0.7,
                "latency_weight": 0.2,
                "freshness_weight": 0.1,
                "latency_budget_ms": 4000,
                "freshness_ttl_h": 12,
                "min_score": 33,
            },
            "eligibility": {"error_cooldown_s": 42},
        }
    )
    try:
        assert reg._weights.availability == 0.7
        assert reg._weights.latency_budget_ms == 4000
        assert reg._weights.freshness_ttl_h == 12
        assert reg._min_score == 33
        assert reg._error_cooldown_s == 42
    finally:
        reg.close()


def test_prune_history_deletes_only_old_closed_rows(registry):
    _seed(registry)
    lease = registry.claim("87.120.102.53", station=6, ttl_s=300, now=1000.0)
    registry.record(lease, ProbeResult(status=200, tested_at=1001.0))
    registry.assign(lease, now=1002.0)
    registry.release(lease, "rotation", now=1003.0)

    result = registry.prune_history(retention_days=1, now=1003.0 + 86400.0 * 2)
    assert result["observations"] == 1
    assert result["assignments"] == 1
    assert registry._conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"] == 0
    assert registry._conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"] == 0

    # An open assignment is never pruned, even when old.
    lease2 = registry.claim("87.120.102.53", station=6, ttl_s=300, now=300000.0)
    registry.record(lease2, ProbeResult(status=200, tested_at=300001.0))
    registry.assign(lease2, now=300002.0)
    result = registry.prune_history(retention_days=1, now=400000.0)
    assert result["assignments"] == 0


def test_assign_observed_tests_then_keeps_the_ip(registry):
    record = registry.assign_observed(
        egress_ip="87.120.102.53",
        hostname="al110.nordvpn.com",
        country="Albania",
        station=6,
        latency_ms=120.0,
        tested_at=1000.0,
    )
    assert record is not None
    assert record.state is IpState.ASSIGNED
    assert record.assigned_station == 6
    assert record.last_status == 200
    obs = registry._conn.execute(
        "SELECT status FROM observations WHERE egress_ip=?", ("87.120.102.53",)
    ).fetchall()
    assert [int(r["status"]) for r in obs] == [200]
    open_assignments = registry._conn.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE egress_ip=? AND released_at IS NULL",
        ("87.120.102.53",),
    ).fetchone()["n"]
    assert open_assignments == 1


def test_assign_observed_replaces_the_station_ip(registry):
    first = registry.assign_observed(
        egress_ip="87.120.102.53",
        hostname="al110.nordvpn.com",
        country="Albania",
        station=6,
        tested_at=1000.0,
    )
    assert first is not None
    second = registry.assign_observed(
        egress_ip="87.120.102.54",
        hostname="al111.nordvpn.com",
        country="Albania",
        station=6,
        tested_at=1010.0,
    )
    assert second is not None and second.egress_ip == "87.120.102.54"
    old = registry.get("87.120.102.53")
    assert old.state is IpState.CANDIDATE
    assert old.assigned_station is None
    assert registry.assigned_for_station(6).egress_ip == "87.120.102.54"
    open_assignments = registry._conn.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE station=6 AND released_at IS NULL"
    ).fetchone()["n"]
    assert open_assignments == 1


def test_assign_observed_never_steals_another_station_ip(registry):
    registry.assign_observed(
        egress_ip="87.120.102.53",
        hostname="al110.nordvpn.com",
        country="Albania",
        station=1,
        tested_at=1000.0,
    )
    stolen = registry.assign_observed(
        egress_ip="87.120.102.53",
        hostname="al110.nordvpn.com",
        country="Albania",
        station=2,
        tested_at=1001.0,
    )
    assert stolen is None
    assert registry.get("87.120.102.53").assigned_station == 1
