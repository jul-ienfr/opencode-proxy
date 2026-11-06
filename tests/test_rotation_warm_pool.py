"""WarmPool tests: TTL, atomic promotion, replenishment target."""

from __future__ import annotations

from rotation.application.warm_pool import WarmPool
from rotation.infrastructure.db.migrate import apply_schema, connect
from rotation.infrastructure.db.repositories import UnitOfWork


def _schema(tmp_path):
    conn = connect(tmp_path / "registry.db")
    apply_schema(conn, now=1000.0)
    conn.close()


def test_warm_pool_promotes_atomically_and_reports_missing(tmp_path):
    _schema(tmp_path)
    now = {"t": 1000.0}
    with UnitOfWork(tmp_path / "registry.db") as uow:
        pool = WarmPool(uow.repo, desired=3, ttl_s=600, clock=lambda: now["t"])
        uow.repo.upsert_ip(ip="1.1.1.1", score=10, now=now["t"])
        uow.repo.upsert_ip(ip="2.2.2.2", score=20, now=now["t"])
        pool.put(
            ip="1.1.1.1",
            hostname="a.nordvpn.com",
            transport="wireguard",
            score=10,
        )
        pool.put(
            ip="2.2.2.2",
            hostname="b.nordvpn.com",
            transport="wireguard",
            score=20,
        )
        assert pool.status().ready == 2
        assert pool.status().missing == 1

        first = pool.take()
        assert first["ip"] == "2.2.2.2"
        second = pool.take()
        assert second["ip"] == "1.1.1.1"
        assert pool.take() is None
        assert pool.status().ready == 0


def test_warm_pool_expires_stale_spares(tmp_path):
    _schema(tmp_path)
    now = {"t": 1000.0}
    with UnitOfWork(tmp_path / "registry.db") as uow:
        pool = WarmPool(uow.repo, desired=1, ttl_s=60, clock=lambda: now["t"])
        uow.repo.upsert_ip(ip="3.3.3.3", now=now["t"])
        pool.put(ip="3.3.3.3", hostname="c.nordvpn.com", transport="openvpn-udp")
        assert pool.status().ready == 1
        now["t"] = 1100.0
        assert pool.status().ready == 0
        assert pool.expire() == 1
        assert pool.take() is None
