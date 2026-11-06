"""Legacy ip_registry.db -> registry.db migration."""

from __future__ import annotations

import sqlite3

from rotation.infrastructure.db.maintenance import run_once


def _legacy(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ip_state("
        "egress_ip TEXT, hostname TEXT, country TEXT, state TEXT, score REAL, "
        "last_status INTEGER, last_tested_at REAL, last_ok_at REAL, "
        "reset_at REAL, error_until REAL, latency_ms REAL, tests INTEGER, "
        "successes INTEGER, failures INTEGER, assigned_station INTEGER, "
        "assigned_at REAL, lease_id TEXT, lease_expires_at REAL, updated_at REAL)"
    )
    conn.execute(
        "CREATE TABLE verified_spares("
        "hostname TEXT, country TEXT, egress_ip TEXT, model TEXT, "
        "transport TEXT, station INTEGER, last_200_at REAL, expires_at REAL)"
    )
    now = 1793000000.0
    conn.execute(
        "INSERT INTO ip_state VALUES("
        "'1.1.1.1','a.nordvpn.com','Germany','assigned',10,200,?,?,NULL,0,"
        "100,1,1,0,1,?, '',0,?)",
        (now, now, now, now),
    )
    conn.execute(
        "INSERT INTO verified_spares VALUES("
        "'a.nordvpn.com','Germany','1.1.1.1','m','wireguard',1,?,?)",
        (now, now + 3600),
    )
    conn.commit()
    conn.close()


def test_legacy_ip_registry_is_migrated_into_registry_db(tmp_path):
    source = tmp_path / "ip_registry.db"
    target = tmp_path / "registry.db"
    _legacy(source)
    result = run_once(target, backup_dir=None, ip_source=source, now=1793000001.0)

    assert result["legacy_ips"]["servers"] == 1
    assert result["legacy_ips"]["ips"] == 1
    assert result["legacy_ips"]["spares"] == 1

    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM egress_ips").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ip_server_map").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM warm_pool").fetchone()[0] == 1
        row = conn.execute(
            "SELECT country, state FROM egress_ips WHERE ip='1.1.1.1'"
        ).fetchone()
        assert row == ("Germany", "fresh")
    finally:
        conn.close()
