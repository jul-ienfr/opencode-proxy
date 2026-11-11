"""Claims tables in the target registry.

`CatalogClaims` historically owned `logs/catalog_claims.db`. The migration
puts the same tables in `logs/registry.db` and copies the existing rows once,
so the runtime has one database file instead of two.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    hostname TEXT PRIMARY KEY,
    country TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claims (
    hostname TEXT PRIMARY KEY,
    station INTEGER NOT NULL,
    status TEXT NOT NULL,
    egress_ip TEXT,
    retry_after REAL NOT NULL DEFAULT 0,
    reset_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    updated_at REAL NOT NULL,
    lease_until REAL NOT NULL,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_status
    ON claims(status, reset_at);
CREATE TABLE IF NOT EXISTS rebind_budget (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_start REAL NOT NULL,
    count INTEGER NOT NULL
);

"""

_TABLES = ("catalog", "claims", "rebind_budget")


def ensure_claims_schema(target: str | Path) -> None:
    conn = sqlite3.connect(str(target), timeout=10.0)
    try:
        conn.executescript(CLAIMS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def migrate_claims(target: str | Path, source: str | Path) -> dict[str, int]:
    """Copy legacy claims rows into the target; idempotent."""

    target = Path(target)
    source = Path(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_claims_schema(target)
    if not source.exists() or source.resolve() == target.resolve():
        return dict.fromkeys(_TABLES, 0)
    copied: dict[str, int] = {}
    conn = sqlite3.connect(str(target), timeout=10.0)
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        for table in _TABLES:
            try:
                exists = conn.execute(
                    "SELECT 1 FROM legacy.sqlite_master "
                    "WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
            except sqlite3.DatabaseError:
                exists = None
            if not exists:
                copied[table] = 0
                continue
            cur = conn.execute(
                f"INSERT OR IGNORE INTO main.{table} SELECT * FROM legacy.{table}"
            )
            copied[table] = int(cur.rowcount or 0)
        conn.commit()
    finally:
        try:
            conn.execute("DETACH DATABASE legacy")
        except Exception:
            pass
        conn.close()
    return copied
