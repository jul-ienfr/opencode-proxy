"""SQLite schema and connection helpers for the IP registry.

Schema v2 adds catalog provenance to ``servers`` (``source_id``, ``stale``,
``catalog_fetched_at``) and a ``catalog_snapshots`` journal.  Migration is
idempotent: existing v1 databases keep their rows and gain the columns in
place, with no destructive rebuild.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servers (
    hostname           TEXT PRIMARY KEY,
    server_ip          TEXT,
    country            TEXT NOT NULL,
    city               TEXT,
    load               REAL NOT NULL DEFAULT 0,
    technologies       TEXT NOT NULL DEFAULT '[]',
    first_seen_at      REAL NOT NULL,
    last_seen_at       REAL NOT NULL,
    active             INTEGER NOT NULL DEFAULT 1,
    source_id          TEXT NOT NULL DEFAULT 'nordvpn_api_servers',
    stale              INTEGER NOT NULL DEFAULT 0,
    catalog_fetched_at REAL
);
CREATE INDEX IF NOT EXISTS idx_servers_country
    ON servers(country, active);

CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname       TEXT NOT NULL,
    egress_ip      TEXT,
    station        INTEGER,
    tested_at      REAL NOT NULL,
    status         INTEGER NOT NULL,
    retry_after_s  REAL NOT NULL DEFAULT 0,
    reset_at       REAL,
    latency_ms     REAL,
    error          TEXT,
    attempt_id     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_hostname_time
    ON observations(hostname, tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_egress_time
    ON observations(egress_ip, tested_at DESC);

CREATE TABLE IF NOT EXISTS ip_state (
    egress_ip        TEXT PRIMARY KEY,
    hostname         TEXT NOT NULL,
    country          TEXT NOT NULL,
    state            TEXT NOT NULL,
    score            REAL NOT NULL DEFAULT 0,
    last_status      INTEGER,
    last_tested_at   REAL,
    last_ok_at       REAL,
    reset_at         REAL,
    error_until      REAL,
    latency_ms       REAL,
    tests            INTEGER NOT NULL DEFAULT 0,
    successes        INTEGER NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0,
    assigned_station INTEGER,
    assigned_at      REAL,
    lease_id         TEXT,
    lease_expires_at REAL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_eligible
    ON ip_state(state, reset_at, error_until, score DESC);
CREATE INDEX IF NOT EXISTS idx_state_country
    ON ip_state(country, state, score DESC);
CREATE INDEX IF NOT EXISTS idx_state_station
    ON ip_state(assigned_station);
-- One egress IP can be assigned to at most one station.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_assigned_per_ip
    ON ip_state(egress_ip)
    WHERE state = 'assigned';
-- A station can hold at most one assigned IP at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_assigned_per_station
    ON ip_state(assigned_station)
    WHERE state = 'assigned' AND assigned_station IS NOT NULL;

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    egress_ip    TEXT NOT NULL,
    hostname     TEXT NOT NULL,
    station      INTEGER NOT NULL,
    assigned_at  REAL NOT NULL,
    released_at  REAL,
    reason       TEXT,
    result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_assign_station
    ON assignments(station, assigned_at DESC);
"""

# Applied after the ALTER TABLE migration so an old ``servers`` table never
# blocks index creation.
EXTRA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,
    fetched_at   REAL NOT NULL,
    etag         TEXT,
    stale        INTEGER NOT NULL DEFAULT 0,
    server_count INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_snapshots_time
    ON catalog_snapshots(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_servers_source
    ON servers(source_id, active);
"""

_PROVENANCE_COLUMNS = {
    "source_id": "TEXT NOT NULL DEFAULT 'nordvpn_api_servers'",
    "stale": "INTEGER NOT NULL DEFAULT 0",
    "catalog_fetched_at": "REAL",
}


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a WAL SQLite connection with sane pragmas.

    Callers own the connection and must close it.  Use short transactions and
    never perform network I/O while a write transaction is open.
    """

    target = str(path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database to ``SCHEMA_VERSION`` without data loss."""

    columns = _table_columns(conn, "servers")
    for name, sql_type in _PROVENANCE_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE servers ADD COLUMN {name} {sql_type}")
    conn.executescript(EXTRA_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes, migrate, and record the schema version."""

    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.commit()


def ensure_schema(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except Exception:
        return 0
