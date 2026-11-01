"""SQLite schema and connection helpers for the IP registry.

Phase 0/1 foundation: schema, pragmas, idempotent initialization.  The
repository implementation (claim/assign/release) lives in a later phase.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servers (
    hostname       TEXT PRIMARY KEY,
    server_ip      TEXT,
    country        TEXT NOT NULL,
    city           TEXT,
    load           REAL NOT NULL DEFAULT 0,
    technologies   TEXT NOT NULL DEFAULT '[]',
    first_seen_at  REAL NOT NULL,
    last_seen_at   REAL NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1
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


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes and record the schema version (idempotent)."""

    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
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
