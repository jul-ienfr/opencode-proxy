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
    # [024 T201] Tables mesurées les plus grosses (registry.db) : elles
    # étaient absentes de la purge d'origine et grossissaient sans borne.
    "egress_observations_days": 30,
    "egress_threat_intel_days": 30,
    "scan_attempts_days": 30,
    "qualification_attempts_days": 30,
    "egress_options_days": 30,
    "qualification_epochs_days": 30,
}

#: (table, colonne temporelle, clé de durée). `server_transports` est un ÉTAT
#: (PK hostname+transport) et ne doit jamais être purgé par âge.
PRUNE_TABLES = (
    ("observations", "tested_at", "observations_days"),
    ("fallback_events", "started_at", "fallback_days"),
    ("incidents", "started_at", "incidents_days"),
    ("repair_jobs", "started_at", "repair_jobs_days"),
    ("config_snapshots", "created_at", "config_days"),
    ("egress_observations", "observed_at", "egress_observations_days"),
    ("egress_threat_intel", "last_seen", "egress_threat_intel_days"),
    ("scan_attempts", "started_at", "scan_attempts_days"),
    ("qualification_attempts", "started_at", "qualification_attempts_days"),
    ("egress_options", "last_seen_at", "egress_options_days"),
    ("qualification_epochs", "started_at", "qualification_epochs_days"),
)


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
    for table, key, days_key in PRUNE_TABLES:
        cutoff = ts - max(0.0, float(cfg[days_key])) * 86400.0
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE {key} < ?", (cutoff,))
        except sqlite3.OperationalError:
            # Table absente d'une base plus ancienne : rien à purger.
            deleted[table] = 0
            continue
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
