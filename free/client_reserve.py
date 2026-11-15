"""Réserve client d'IPs prouvées 200 (022 T001/T002).

Une IP n'entre dans cette table qu'après un 200 réel. Le pool client ne sert
que des IPs de cette réserve ; les probes l'ignorent. La table vit dans
``registry.db`` et survit aux redémarrages.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "logs" / "registry.db"
DEFAULT_TTL_S = 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_reserve (
    ip TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    station INTEGER,
    verified_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'chain-200'
);
CREATE INDEX IF NOT EXISTS idx_client_reserve_expires
    ON client_reserve(expires_at);
"""

_CACHE: tuple[float, set[str]] | None = None
_CACHE_TTL_S = 2.0


def _connect(db: Path | None = None) -> sqlite3.Connection:
    return sqlite3.connect(str(db or DEFAULT_DB), timeout=5.0)


def ensure_schema(db: Path | None = None) -> None:
    with _connect(db) as conn:
        conn.executescript(_SCHEMA)


def add(
    ip: str,
    hostname: str,
    station: int | None = None,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    source: str = "chain-200",
    now: float | None = None,
    db: Path | None = None,
) -> bool:
    target = str(ip or "").strip()
    host = str(hostname or "").strip()
    if not target or not host:
        return False
    ts = time.time() if now is None else float(now)
    ttl = max(60.0, float(ttl_s or DEFAULT_TTL_S))
    try:
        ensure_schema(db)
        with _connect(db) as conn:
            conn.execute(
                "INSERT INTO client_reserve"
                "(ip, hostname, station, verified_at, expires_at, source) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(ip) DO UPDATE SET "
                "hostname=excluded.hostname, station=excluded.station, "
                "verified_at=excluded.verified_at, "
                "expires_at=excluded.expires_at, source=excluded.source",
                (
                    target,
                    host,
                    int(station) if station else None,
                    ts,
                    ts + ttl,
                    str(source or "chain-200"),
                ),
            )
            conn.commit()
        _invalidate()
        return True
    except Exception:
        return False


def _invalidate() -> None:
    global _CACHE
    _CACHE = None


def reserved_ips(
    *, now: float | None = None, db: Path | None = None
) -> set[str]:
    global _CACHE
    ts = time.time() if now is None else float(now)
    if db is None and _CACHE is not None:
        cached_at, cached = _CACHE
        if ts - cached_at < _CACHE_TTL_S:
            return set(cached)
    try:
        ensure_schema(db)
        with _connect(db) as conn:
            try:
                rows = conn.execute(
                    "SELECT c.ip FROM client_reserve c "
                    "WHERE c.expires_at > ? AND NOT EXISTS ("
                    "SELECT 1 FROM quota_buckets q WHERE q.ip = c.ip "
                    "AND q.last_status = 429 AND q.reset_at > ?)",
                    (ts, ts),
                ).fetchall()
            except sqlite3.OperationalError:
                # registry.db sans quota_buckets (tests isolés) : pas de filtre.
                rows = conn.execute(
                    "SELECT ip FROM client_reserve WHERE expires_at > ?", (ts,)
                ).fetchall()
        out = {str(row[0]) for row in rows if row and row[0]}
    except Exception:
        return set()
    if db is None:
        _CACHE = (ts, out)
    return set(out)


def is_reserved(ip: str, *, now: float | None = None) -> bool:
    return str(ip or "").strip() in reserved_ips(now=now)


def remove(ip: str, *, db: Path | None = None) -> int:
    target = str(ip or "").strip()
    if not target:
        return 0
    try:
        with _connect(db) as conn:
            cur = conn.execute("DELETE FROM client_reserve WHERE ip=?", (target,))
            conn.commit()
        _invalidate()
        return int(cur.rowcount or 0)
    except Exception:
        return 0


def prune(*, now: float | None = None, db: Path | None = None) -> int:
    ts = time.time() if now is None else float(now)
    try:
        with _connect(db) as conn:
            cur = conn.execute(
                "DELETE FROM client_reserve WHERE expires_at <= ?", (ts,)
            )
            conn.commit()
        _invalidate()
        return int(cur.rowcount or 0)
    except Exception:
        return 0


def count(*, now: float | None = None, db: Path | None = None) -> int:
    return len(reserved_ips(now=now, db=db))
