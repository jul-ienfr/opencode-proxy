"""Runtime assignments and leases on the target registry schema."""

from __future__ import annotations

import sqlite3

from free.ip_registry.registry_db import (
    assign_observed,
    assigned_for_station,
    reconcile,
    release_station,
)
from rotation.infrastructure.db.migrate import apply_schema, connect

NOW = 1_793_000_000.0


def _db(tmp_path):
    path = tmp_path / "registry.db"
    conn = connect(path)
    apply_schema(conn, now=NOW)
    conn.close()
    return path


def test_assign_observed_creates_one_active_lease(tmp_path):
    db = _db(tmp_path)
    record = assign_observed(
        egress_ip="1.1.1.1",
        hostname="a.nordvpn.com",
        country="Germany",
        station=1,
        latency_ms=120.0,
        tested_at=NOW,
        db_path=db,
    )
    assert record is not None
    lease = assigned_for_station(1, db_path=db)
    assert lease["ip"] == "1.1.1.1"
    assert lease["hostname"] == "a.nordvpn.com"
    assert lease["country"] == "Germany"

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT state FROM egress_ips WHERE ip='1.1.1.1'"
        ).fetchone()[0] == "assigned"
        assert conn.execute(
            "SELECT COUNT(*) FROM leases WHERE status='active'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE status=200 AND station=1"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_assign_observed_never_steals_an_active_ip(tmp_path):
    db = _db(tmp_path)
    assert assign_observed(
        egress_ip="1.1.1.1",
        hostname="a.nordvpn.com",
        station=1,
        tested_at=NOW,
        db_path=db,
    )
    assert assign_observed(
        egress_ip="1.1.1.1",
        hostname="b.nordvpn.com",
        station=2,
        tested_at=NOW,
        db_path=db,
    ) is None
    assert assigned_for_station(2, db_path=db) is None


def test_release_station_makes_the_ip_available_again(tmp_path):
    db = _db(tmp_path)
    assign_observed(
        egress_ip="2.2.2.2",
        hostname="a.nordvpn.com",
        station=1,
        tested_at=NOW,
        db_path=db,
    )
    assert release_station(1, "live_mismatch", now=NOW, db_path=db) == 1
    assert assigned_for_station(1, db_path=db) is None
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT state FROM egress_ips WHERE ip='2.2.2.2'"
        ).fetchone()[0] == "fresh"
    finally:
        conn.close()


def test_reconcile_revives_ips_whose_reset_has_passed(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, "
            "last_429_at, reset_at, state) VALUES('5.5.5.5', ?, ?, ?, ?, 'exhausted')",
            (NOW, NOW, NOW, NOW - 10),
        )
        conn.execute(
            "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, "
            "last_429_at, reset_at, state) VALUES('6.6.6.6', ?, ?, ?, ?, 'exhausted')",
            (NOW, NOW, NOW, NOW + 3600),
        )
        conn.commit()
    finally:
        conn.close()
    result = reconcile(now=NOW, db_path=db)
    assert result["revived_ips"] == 1
    conn = sqlite3.connect(db)
    try:
        states = dict(conn.execute("SELECT ip, state FROM egress_ips"))
    finally:
        conn.close()
    assert states["5.5.5.5"] == "fresh"
    assert states["6.6.6.6"] == "exhausted"


def test_reconcile_expires_leases_and_orphan_assignments(tmp_path):
    db = _db(tmp_path)
    assert assign_observed(
        egress_ip="3.3.3.3",
        hostname="a.nordvpn.com",
        station=1,
        tested_at=NOW,
        db_path=db,
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE leases SET expires_at=? WHERE status='active'", (NOW + 10,)
        )
        conn.execute(
            "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, state) "
            "VALUES('4.4.4.4', ?, ?, 'assigned')",
            (NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    result = reconcile(now=NOW + 20, db_path=db)
    assert result["released_leases"] == 1
    assert result["released_ips"] == 1
    assert "revived_ips" in result
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM leases WHERE status='active'"
        ).fetchone()[0] == 0
        states = dict(conn.execute("SELECT ip, state FROM egress_ips"))
        assert states["3.3.3.3"] == "fresh"
        assert states["4.4.4.4"] == "fresh"
    finally:
        conn.close()
