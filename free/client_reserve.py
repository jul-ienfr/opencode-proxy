"""Réserve client d'IPs prouvées 200 (022 T001/T002).

Une IP n'entre dans cette table qu'après un 200 réel. Le pool client ne sert
que des IPs de cette réserve ; les probes l'ignorent. La table vit dans
``registry.db`` et survit aux redémarrages.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "logs" / "registry.db"
DEFAULT_TTL_S = 3600.0
POISONED_ERROR = "probe-format-poisoned"
POISON_TTL_S = 86400.0


def _default_ttl() -> float:
    """TTL de la preuve de réserve (022 T021).

    Défaut 3600 s. Un TTL de 5 min imposerait ~1 700 re-probes/jour pour six
    IPs, incompatible avec le quota free (~200 requêtes/jour/IP). La fraîcheur
    vient des 200 du cycle et de l'exclusion immédiate sur 429.
    """

    try:
        return max(60.0, float(os.getenv("RESERVE_TTL_S", "") or DEFAULT_TTL_S))
    except (TypeError, ValueError):
        return DEFAULT_TTL_S


def _client_budget() -> int:
    """Budget client par IP (022 T006).

    ``CLIENT_BUDGET_PCT`` (défaut 80) × ``CLIENT_QUOTA_ESTIMATE`` (défaut 200).
    Une IP dont le bucket a consommé ce budget sort de la réserve avant
    épuisement.
    """

    try:
        pct = float(os.getenv("CLIENT_BUDGET_PCT", "80") or 80)
        estimate = float(os.getenv("CLIENT_QUOTA_ESTIMATE", "200") or 200)
        return max(1, int(estimate * pct / 100.0))
    except (TypeError, ValueError):
        return 160


def _probe_budget() -> int:
    """Budget probe par IP (022 T006).

    ``PROBE_BUDGET_PCT`` (défaut 50). Une IP qui a déjà consommé la moitié de
    son quota estimé n'est plus une candidate utile : la sonder gaspille le
    peu de quota qui reste au client.
    """

    try:
        pct = float(os.getenv("PROBE_BUDGET_PCT", "50") or 50)
        estimate = float(os.getenv("CLIENT_QUOTA_ESTIMATE", "200") or 200)
        return max(1, int(estimate * pct / 100.0))
    except (TypeError, ValueError):
        return 100

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
    ttl_s: float | None = None,
    source: str = "chain-200",
    now: float | None = None,
    db: Path | None = None,
) -> bool:
    target = str(ip or "").strip()
    host = str(hostname or "").strip()
    if not target or not host:
        return False
    ts = time.time() if now is None else float(now)
    ttl = max(60.0, float(ttl_s if ttl_s is not None else _default_ttl()))
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


def _excluded_ips(
    conn: sqlite3.Connection, ips: set[str], ts: float, budget: int
) -> set[str]:
    """IPs de la réserve que le ledger interdit de servir (022 T006/T016).

    Trois motifs : 429 actif, budget client consommé, ou probe non conforme
    (`probe-format-poisoned`) de moins de 24 h. Un compteur (bucket) qui a
    répondu 200 depuis le poison n'est pas grillé : le 200 est postérieur.
    """

    if not ips:
        return set()
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(quota_buckets)")
        }
    except sqlite3.DatabaseError:
        return set()
    if not columns:
        return set()
    wanted = [
        name
        for name in (
            "ip",
            "used",
            "last_status",
            "reset_at",
            "last_error",
            "last_seen_at",
        )
        if name in columns
    ]
    if "ip" not in wanted:
        return set()
    placeholders = ",".join("?" for _ in ips)
    rows = conn.execute(
        f"SELECT {', '.join(wanted)} FROM quota_buckets "
        f"WHERE ip IN ({placeholders})",
        tuple(ips),
    ).fetchall()
    excluded: set[str] = set()
    for values in rows:
        row = dict(zip(wanted, values))
        ip = str(row.get("ip") or "")
        if not ip:
            continue
        status = row.get("last_status")
        reset_at = float(row.get("reset_at") or 0.0)
        used = row.get("used")
        if status == 429 and reset_at > ts:
            excluded.add(ip)
            continue
        if used is not None and int(used) >= budget:
            excluded.add(ip)
            continue
        if str(row.get("last_error") or "") == POISONED_ERROR:
            seen = float(row.get("last_seen_at") or 0.0)
            if status != 200 and seen > ts - POISON_TTL_S:
                excluded.add(ip)
    return excluded


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
            rows = conn.execute(
                "SELECT ip FROM client_reserve WHERE expires_at > ?", (ts,)
            ).fetchall()
            out = {str(row[0]) for row in rows if row and row[0]}
            out -= _excluded_ips(conn, out, ts, _client_budget())
    except Exception:
        return set()
    if db is None:
        _CACHE = (ts, out)
    return set(out)


def is_reserved(ip: str, *, now: float | None = None) -> bool:
    return str(ip or "").strip() in reserved_ips(now=now)


def probe_blocked_ips(
    *, now: float | None = None, db: Path | None = None
) -> set[str]:
    """IPs qu'une probe ne doit plus consommer (022 T006).

    Une IP qui a déjà dépensé ``probe_budget_pct`` de son quota estimé n'a plus
    assez pour le client : la sonder ne ferait que confirmer son épuisement.
    """

    del now  # le budget ne dépend pas de l'heure, seulement du compteur
    try:
        ensure_schema(db)
        with _connect(db) as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(quota_buckets)")
            }
            if not {"ip", "used"} <= columns:
                return set()
            rows = conn.execute(
                "SELECT ip FROM quota_buckets WHERE used >= ?",
                (_probe_budget(),),
            ).fetchall()
        return {str(row[0]) for row in rows if row and row[0]}
    except Exception:
        return set()


def probe_budget_remaining(
    *, now: float | None = None, db: Path | None = None
) -> int:
    """Requêtes encore disponibles dans le budget probe des IPs réservées."""

    reserved = reserved_ips(now=now, db=db)
    if not reserved:
        return 0
    budget = _probe_budget()
    try:
        with _connect(db) as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(quota_buckets)")
            }
            if not {"ip", "used"} <= columns:
                return budget * len(reserved)
            placeholders = ",".join("?" for _ in reserved)
            rows = conn.execute(
                f"SELECT ip, MAX(used) FROM quota_buckets "
                f"WHERE ip IN ({placeholders}) GROUP BY ip",
                tuple(reserved),
            ).fetchall()
        used = {str(row[0]): int(row[1] or 0) for row in rows}
        return sum(max(0, budget - used.get(ip, 0)) for ip in reserved)
    except Exception:
        return 0


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
