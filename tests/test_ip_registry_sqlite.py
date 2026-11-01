"""SQLite schema tests for the IP registry (Phase 0)."""

from __future__ import annotations

import sqlite3

import pytest

from free.ip_registry.sqlite import SCHEMA_VERSION, ensure_schema, schema_version


def test_schema_is_created(tmp_path):
    conn = ensure_schema(tmp_path / "registry.db")
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"schema_meta", "servers", "observations", "ip_state", "assignments"} <= tables
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_state_eligible" in indexes
        assert "idx_one_assigned_per_station" in indexes
        assert schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_one_assigned_ip_per_station(tmp_path):
    conn = ensure_schema(tmp_path / "registry.db")
    try:
        now = 1000.0
        conn.execute(
            "INSERT INTO ip_state(egress_ip, hostname, country, state, score, assigned_station, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("87.120.102.53", "a.nordvpn.com", "Albania", "assigned", 90.0, 6, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ip_state(egress_ip, hostname, country, state, score, assigned_station, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("87.120.102.54", "b.nordvpn.com", "Albania", "assigned", 90.0, 6, now),
            )
    finally:
        conn.close()
