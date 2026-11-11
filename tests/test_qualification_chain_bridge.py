"""Contract: real fresh_chain attempts feed qualification transport health."""

from __future__ import annotations

import sqlite3

from free.qualification.chain_bridge import record_chain_attempt


def _rows(db, sql, params=()):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def test_success_attempt_updates_transport_health(tmp_path):
    db = tmp_path / "registry.db"
    attempt_id = record_chain_attempt(
        station=2,
        hostname="al1.nordvpn.com",
        transport="openvpn-udp",
        status="200",
        egress_ip="1.2.3.4",
        latency_ms=250.0,
        db_path=db,
    )
    assert attempt_id.startswith("att_")
    rows = _rows(db, "SELECT status, transport, egress_ip FROM qualification_attempts")
    assert rows == [
        {"status": "200", "transport": "openvpn-udp", "egress_ip": "1.2.3.4"}
    ]
    health = _rows(
        db,
        "SELECT attempts, successes, degraded FROM transport_health "
        "WHERE transport='openvpn-udp' AND station=0",
    )
    assert health == [{"attempts": 1, "successes": 1, "degraded": 0}]


def test_failed_attempt_is_counted_without_success(tmp_path):
    db = tmp_path / "registry.db"
    record_chain_attempt(
        station=3,
        hostname="al2.nordvpn.com",
        transport="openvpn-tcp",
        status="bind_error",
        error="AUTH_FAILED",
        db_path=db,
    )
    health = _rows(
        db,
        "SELECT attempts, successes, infra FROM transport_health "
        "WHERE transport='openvpn-tcp' AND station=0",
    )
    assert health == [{"attempts": 1, "successes": 0, "infra": 1}]


def test_same_round_is_deduplicated_but_a_confirmation_round_is_kept(tmp_path):
    db = tmp_path / "registry.db"
    base = {
        "station": 1,
        "hostname": "al3.nordvpn.com",
        "transport": "wireguard",
        "status": "200",
        "egress_ip": "5.6.7.8",
        "db_path": db,
    }
    assert record_chain_attempt(**base) != ""
    assert record_chain_attempt(**base) == ""
    assert record_chain_attempt(**{**base, "round_n": 1}) != ""
    count = _rows(db, "SELECT COUNT(*) AS n FROM qualification_attempts")
    assert count[0]["n"] == 2
