"""Repository/unit-of-work tests for the target registry."""

from __future__ import annotations

from rotation.infrastructure.db.migrate import apply_schema, connect
from rotation.infrastructure.db.repositories import UnitOfWork


def _schema(tmp_path):
    conn = connect(tmp_path / "registry.db")
    apply_schema(conn, now=1000.0)
    conn.close()


def test_repository_upserts_and_claims_a_unique_ip(tmp_path):
    _schema(tmp_path)
    with UnitOfWork(tmp_path / "registry.db") as uow:
        uow.repo.upsert_source("api", "api", fetched_at=1000.0)
        uow.repo.upsert_server(
            hostname="a.nordvpn.com",
            country="Germany",
            source_id="api",
            technologies=["wireguard", "openvpn-tcp"],
            endpoint_ip="1.2.3.4",
            now=1000.0,
        )
        uow.repo.upsert_ip(ip="9.9.9.9", country="Germany", score=80.0, now=1000.0)
        available = uow.repo.available_ips(now=1000.0)
        assert [row["ip"] for row in available] == ["9.9.9.9"]
        lease = uow.repo.claim_lease(
            station=1,
            ip="9.9.9.9",
            hostname="a.nordvpn.com",
            transport="wireguard",
            now=1000.0,
        )
        assert lease
        assert uow.repo.available_ips(now=1000.0) == []
        assert uow.repo.claim_lease(
            station=2,
            ip="9.9.9.9",
            hostname="a.nordvpn.com",
            transport="wireguard",
            now=1000.0,
        ) is None
        assert uow.repo.release_lease(lease, now=1001.0) is True
        assert [row["ip"] for row in uow.repo.available_ips(now=1001.0)] == ["9.9.9.9"]


def test_repository_scores_and_repair_queue(tmp_path):
    _schema(tmp_path)
    with UnitOfWork(tmp_path / "registry.db") as uow:
        uow.repo.record_observation(
            hostname="a.nordvpn.com",
            transport="wireguard",
            status=200,
            ip="9.9.9.9",
            latency_ms=120.0,
            tested_at=1000.0,
        )
        assert uow.repo.compute_scores(now=1000.0) >= 1
        row = uow.repo._conn.execute(
            "SELECT COUNT(*) FROM score_snapshots"
        ).fetchone()
        assert row[0] >= 1

        uow.repo.request_repair(
            station=2, reason="quota-429", model="m", ip="9.9.9.9", now=1000.0
        )
        pending = uow.repo.pending_repairs(now=1001.0)
        assert pending[0]["station"] == 2
        uow.repo.complete_repair(2)
        assert uow.repo.pending_repairs(now=1001.0) == []
