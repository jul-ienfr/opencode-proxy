"""Restart and six-station uniqueness on the target registry (T055/T056)."""

from __future__ import annotations

import sqlite3

from free.chain_state import load_state, save_state
from free.ip_registry.registry_db import (
    assign_observed,
    assigned_for_station,
    reconcile,
)
from rotation.infrastructure.db.migrate import apply_schema, connect

NOW = 1_793_000_000.0


def _db(tmp_path):
    path = tmp_path / "registry.db"
    conn = connect(path)
    apply_schema(conn, now=NOW)
    conn.close()
    return path


def test_chain_resume_keeps_index_and_winner_after_restart(tmp_path):
    state_path = tmp_path / "fresh_chain_state_3_auto.json"
    state = {
        "index": 42,
        "winner": {"host": "a.nordvpn.com", "ip": "1.1.1.1"},
        "results": [{"host": "a", "status": 429, "tested_at": NOW}],
    }
    save_state(state_path, state)
    assert load_state(state_path) == state


def test_six_stations_get_six_distinct_active_leases(tmp_path):
    db = _db(tmp_path)
    for sid in range(1, 7):
        assert assign_observed(
            egress_ip=f"10.0.0.{sid}",
            hostname=f"s{sid}.nordvpn.com",
            country="Germany",
            station=sid,
            tested_at=NOW,
            db_path=db,
        )
        assert assigned_for_station(sid, db_path=db)["ip"] == f"10.0.0.{sid}"

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT station, ip FROM leases WHERE status='active' ORDER BY station"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 6
    assert len({ip for _sid, ip in rows}) == 6


def test_an_ip_is_never_assigned_to_two_stations(tmp_path):
    db = _db(tmp_path)
    assert assign_observed(
        egress_ip="10.0.0.1",
        hostname="a.nordvpn.com",
        station=1,
        tested_at=NOW,
        db_path=db,
    )
    assert assign_observed(
        egress_ip="10.0.0.1",
        hostname="a.nordvpn.com",
        station=2,
        tested_at=NOW,
        db_path=db,
    ) is None
    assert assigned_for_station(2, db_path=db) is None


def test_restart_releases_expired_leases_but_keeps_live_ones(tmp_path):
    db = _db(tmp_path)
    assert assign_observed(
        egress_ip="10.0.0.1",
        hostname="a.nordvpn.com",
        station=1,
        tested_at=NOW,
        db_path=db,
    )
    assert assign_observed(
        egress_ip="10.0.0.2",
        hostname="b.nordvpn.com",
        station=2,
        tested_at=NOW,
        db_path=db,
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE leases SET expires_at=? WHERE station=2 AND status='active'",
            (NOW - 1,),
        )
        conn.commit()
    finally:
        conn.close()
    result = reconcile(now=NOW, db_path=db)
    assert result["released_leases"] == 1
    assert assigned_for_station(1, db_path=db)["ip"] == "10.0.0.1"
    assert assigned_for_station(2, db_path=db) is None
