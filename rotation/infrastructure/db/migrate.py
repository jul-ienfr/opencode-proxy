"""Apply the target registry schema to a SQLite database."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# [feature 007] Colonnes additives pour le provider/egress. ``ALTER TABLE``
# est idempotent : on ne touche qu'aux colonnes absentes.
_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "egress_ips": (
        ("provider", "TEXT NOT NULL DEFAULT 'warp'"),
        ("block_key", "TEXT"),
        ("quota_axis", "TEXT"),
    ),
    "observations": (("provider", "TEXT NOT NULL DEFAULT ''"),),
    "quota_buckets": (("provider", "TEXT NOT NULL DEFAULT ''"),),
}


def _ensure_additive_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDITIVE_COLUMNS.items():
        try:
            existing = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.Error:
            continue
        for name, ddl in columns:
            if name not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                except sqlite3.Error:
                    continue


def connect(path: str | Path) -> sqlite3.Connection:
    target = str(path)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_schema(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    _ensure_additive_columns(conn)
    ts = time.time() if now is None else float(now)
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES(?,?,?) ON CONFLICT(version) DO NOTHING",
        (1, ts, "initial registry schema"),
    )
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES(?,?,?) ON CONFLICT(version) DO NOTHING",
        (SCHEMA_VERSION, ts, "egress provider columns"),
    )
    conn.commit()
    return SCHEMA_VERSION


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)
