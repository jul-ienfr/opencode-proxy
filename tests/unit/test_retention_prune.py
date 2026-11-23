"""[P2 2026-11-21 — T201] La purge d'âge couvre les tables les plus grosses.

Mesure du 2026-11-21 sur `logs/registry.db` : `egress_observations` 374 891,
`egress_threat_intel` 170 281, `scan_attempts` 72 648,
`qualification_attempts` 52 226, `egress_options` 33 975,
`qualification_epochs` 24 330 — toutes absentes de la purge d'origine.
`server_transports` est un ÉTAT (PK hostname+transport) et ne doit PAS être
purgé par âge.
"""

from __future__ import annotations

import sqlite3

from rotation.infrastructure.db.retention import prune

NOW = 10_000_000.0
DAY = 86_400.0

TABLES = {
    "observations": "tested_at",
    "fallback_events": "started_at",
    "incidents": "started_at",
    "repair_jobs": "started_at",
    "config_snapshots": "created_at",
    "egress_observations": "observed_at",
    "egress_threat_intel": "last_seen",
    "scan_attempts": "started_at",
    "qualification_attempts": "started_at",
    "egress_options": "last_seen_at",
    "qualification_epochs": "started_at",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for table, column in TABLES.items():
        conn.execute(f"CREATE TABLE {table} ({column} REAL)")
        conn.execute(
            f"INSERT INTO {table}({column}) VALUES (?)", (NOW - DAY,)
        )
        conn.execute(
            f"INSERT INTO {table}({column}) VALUES (?)", (NOW - 400 * DAY,)
        )
    conn.execute("CREATE TABLE server_transports (hostname TEXT, transport TEXT)")
    conn.execute("INSERT INTO server_transports VALUES ('h', 'wireguard')")
    conn.commit()
    return conn


def test_prune_removes_old_rows_from_every_measured_big_table():
    conn = _db()
    try:
        deleted = prune(conn, now=NOW)
        for table in TABLES:
            assert deleted[table] == 1, (table, deleted)
            remaining = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert remaining == 1, table
    finally:
        conn.close()


def test_prune_never_touches_state_tables():
    conn = _db()
    try:
        prune(conn, now=NOW)
        assert "server_transports" not in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()[0]
        assert (
            conn.execute("SELECT COUNT(*) FROM server_transports").fetchone()[0] == 1
        )
    finally:
        conn.close()
