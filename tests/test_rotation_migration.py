"""Legacy registry migration + retention/backup tests."""

from __future__ import annotations

import sqlite3

from free.ip_registry.sqlite import connect as legacy_connect, init_schema
from rotation.infrastructure.db.migrate import apply_schema, connect
from rotation.infrastructure.db.migrate_legacy import migrate_legacy
from rotation.infrastructure.db.retention import backup, integrity_ok, prune


def _legacy_db(path):
    conn = legacy_connect(path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO servers(hostname, server_ip, country, city, load, "
        "technologies, first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "a.nordvpn.com",
            "1.2.3.4",
            "Germany",
            "Berlin",
            12,
            "[{'identifier': 'wireguard_udp'}, {'identifier': 'openvpn_tcp'}]",
            1000.0,
            1000.0,
        ),
    )
    conn.execute(
        "INSERT INTO ip_state(egress_ip, hostname, country, state, score, "
        "last_status, last_tested_at, last_ok_at, tests, successes, failures, "
        "assigned_station, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("9.9.9.9", "a.nordvpn.com", "Germany", "assigned", 80.0,
         200, 1000.0, 1000.0, 2, 2, 0, 1, 1000.0),
    )
    conn.execute(
        "INSERT INTO observations(hostname, egress_ip, station, tested_at, "
        "status, retry_after_s, reset_at, latency_ms, error, attempt_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("a.nordvpn.com", "9.9.9.9", 1, 1000.0, 200, 0, None, 120.0, "", "att"),
    )
    conn.execute(
        "INSERT INTO assignments(egress_ip, hostname, station, assigned_at, result) "
        "VALUES(?,?,?,?,?)",
        ("9.9.9.9", "a.nordvpn.com", 1, 1000.0, "200"),
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS verified_spares("
        "hostname TEXT, country TEXT, egress_ip TEXT, model TEXT, transport TEXT, "
        "station INTEGER, last_200_at REAL, expires_at REAL, "
        "PRIMARY KEY(hostname, egress_ip, model))"
    )
    conn.execute(
        "INSERT INTO verified_spares(hostname, country, egress_ip, model, transport, "
        "station, last_200_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
        ("a.nordvpn.com", "Germany", "9.9.9.9", "m", "wireguard", 1, 1000.0, 9999.0),
    )
    conn.commit()
    conn.close()


def test_legacy_migration_preserves_servers_ips_and_leases(tmp_path):
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "registry.db"
    _legacy_db(legacy)
    counts = migrate_legacy(legacy, target, now=2000.0)
    assert counts["servers"] == 1
    assert counts["ips"] == 1
    assert counts["observations"] == 1
    assert counts["warm"] == 1

    conn = connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM server_transports").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM egress_ips").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM leases WHERE status='active'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM warm_pool"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_retention_and_backup(tmp_path):
    db = tmp_path / "registry.db"
    conn = connect(db)
    apply_schema(conn, now=1000.0)
    conn.execute(
        "INSERT INTO observations(hostname, transport, status, tested_at) "
        "VALUES(?,?,?,?)",
        ("a.nordvpn.com", "wireguard", 200, 100.0),
    )
    conn.execute(
        "INSERT INTO observations(hostname, transport, status, tested_at) "
        "VALUES(?,?,?,?)",
        ("a.nordvpn.com", "wireguard", 200, 9_000_000.0),
    )
    conn.commit()
    deleted = prune(conn, now=10_000_000.0)
    assert deleted["observations"] == 1
    assert integrity_ok(conn)
    out = backup(conn, tmp_path / "backup.db")
    assert out.exists() and out.stat().st_size > 0
    conn.close()
    restored = sqlite3.connect(out)
    assert restored.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    restored.close()
