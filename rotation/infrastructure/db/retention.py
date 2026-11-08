"""Retention, integrity and backup helpers for the target registry."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DEFAULTS = {
    "observations_days": 30,
    "fallback_days": 30,
    "incidents_days": 90,
    "repair_jobs_days": 30,
    "config_days": 90,
}


def prune(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    days: dict[str, float] | None = None,
) -> dict[str, int]:
    ts = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS)
    cfg.update(days or {})
    deleted: dict[str, int] = {}
    for table, key, days_key in (
        ("observations", "tested_at", "observations_days"),
        ("fallback_events", "started_at", "fallback_days"),
        ("incidents", "started_at", "incidents_days"),
        ("repair_jobs", "started_at", "repair_jobs_days"),
        ("config_snapshots", "created_at", "config_days"),
    ):
        cutoff = ts - max(0.0, float(cfg[days_key])) * 86400.0
        cur = conn.execute(f"DELETE FROM {table} WHERE {key} < ?", (cutoff,))
        deleted[table] = int(cur.rowcount or 0)
    conn.commit()
    return deleted


def checkpoint(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"


def backup(conn: sqlite3.Connection, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn.execute("VACUUM INTO ?", (str(target),))
    return target
