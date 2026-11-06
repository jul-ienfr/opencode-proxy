"""Target registry schema: tables, constraints and idempotent migration."""

from __future__ import annotations

import sqlite3

import pytest

from rotation.infrastructure.db.migrate import apply_schema, connect, current_version


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "registry.db")
    apply_schema(conn, now=1000.0)
    yield conn
    conn.close()


def _seed_server(conn):
    conn.execute(
        "INSERT INTO catalog_sources(source_id, kind, fetched_at) VALUES(?,?,?)",
        ("api", "api", 1000.0),
    )
    conn.execute(
        "INSERT INTO servers(hostname, country, source_id, first_seen_at, last_seen_at) "
        "VALUES(?,?,?,?,?)",
        ("a.nordvpn.com", "Germany", "api", 1000.0, 1000.0),
    )
    conn.execute(
        "INSERT INTO server_transports(hostname, transport, supported) VALUES(?,?,1)",
        ("a.nordvpn.com", "wireguard"),
    )
    conn.execute(
        "INSERT INTO endpoints(hostname, transport, endpoint_ip, source, "
        "first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?)",
        ("a.nordvpn.com", "wireguard", "1.2.3.4", "api", 1000.0, 1000.0),
    )
    conn.execute(
        "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, state) "
        "VALUES(?,?,?, 'fresh')",
        ("9.9.9.9", 1000.0, 1000.0),
    )
    conn.commit()


def test_schema_is_idempotent_and_versioned(db):
    assert current_version(db) == 1
    apply_schema(db, now=1001.0)
    assert current_version(db) == 1
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "servers",
        "server_transports",
        "endpoints",
        "egress_ips",
        "ip_server_map",
        "observations",
        "quota_buckets",
        "leases",
        "warm_pool",
        "sweep_cursor",
        "repair_jobs",
        "repair_requests",
        "fallback_events",
        "incidents",
        "station_state",
        "score_snapshots",
    }.issubset(tables)


def test_foreign_keys_and_check_constraints(db):
    _seed_server(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO servers(hostname, country, source_id, first_seen_at, last_seen_at) "
            "VALUES(?,?,?,?,?)",
            ("b.nordvpn.com", "France", "missing", 1.0, 1.0),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, state) "
            "VALUES(?,?,?,?)",
            ("8.8.8.8", 1.0, 1.0, "invalid-state"),
        )
    db.rollback()


def test_one_active_lease_per_ip_and_station(db):
    _seed_server(db)
    db.execute(
        "INSERT INTO leases(lease_id, station, ip, hostname, transport, "
        "started_at, renewed_at, expires_at, status) "
        "VALUES('l1', 1, '9.9.9.9', 'a.nordvpn.com', 'wireguard', 1,1,1000,'active')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO leases(lease_id, station, ip, hostname, transport, "
            "started_at, renewed_at, expires_at, status) "
            "VALUES('l2', 2, '9.9.9.9', 'a.nordvpn.com', 'wireguard', 1,1,1000,'active')"
        )
    db.rollback()
