"""Shared catalog claims for parallel fresh-IP chains.

Each station owns its own egress, but all stations share one catalog and one
claims table: a hostname is claimed by exactly one station at a time, failures
are recorded with their reset time, and a global rebind budget keeps the
NordVPN AUTH rate safe while stations search in parallel.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from typing import Any


class CatalogClaims:
    def __init__(
        self,
        path: str,
        *,
        lease_s: float = 300.0,
        rebind_limit_per_min: int = 8,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = str(path)
        self._lease_s = max(1.0, float(lease_s))
        self._rebind_limit = max(1, int(rebind_limit_per_min))
        self._clock = clock
        self._conn = sqlite3.connect(self._path, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(
            """
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
                lease_until REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_claims_status
                ON claims(status, reset_at);
            CREATE TABLE IF NOT EXISTS rebind_budget (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                window_start REAL NOT NULL,
                count INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def seed(self, candidates: Iterable[dict]) -> int:
        rows = []
        for priority, item in enumerate(candidates):
            host = str(item.get("host") or "").strip()
            country = str(item.get("country") or "").strip()
            if host and country:
                rows.append((host, country, priority))
        with self._conn:
            self._conn.executemany(
                "INSERT INTO catalog(hostname, country, priority) VALUES (?, ?, ?) "
                "ON CONFLICT(hostname) DO UPDATE SET "
                "country=excluded.country, priority=excluded.priority",
                rows,
            )
        return len(rows)

    def claim_next(self, station: int, now: float | None = None) -> dict[str, Any] | None:
        ts = self._clock() if now is None else float(now)
        station = int(station)
        with self._conn:
            # An expired lease is reclaimable.
            self._conn.execute(
                "UPDATE claims SET status='abandoned' "
                "WHERE status='claimed' AND lease_until < ?",
                (ts,),
            )
            row = self._conn.execute(
                """
                SELECT c.hostname, c.country
                FROM catalog c
                LEFT JOIN claims cl ON cl.hostname = c.hostname
                WHERE cl.hostname IS NULL
                   OR (cl.status='claimed' AND cl.lease_until < ?)
                   OR (cl.status IN ('no_egress','bind_error','duplicate','abandoned')
                       AND cl.lease_until <= ?)
                   OR (cl.status='429' AND cl.reset_at <= ?)
                ORDER BY c.priority, c.hostname
                LIMIT 1
                """,
                (ts, ts, ts),
            ).fetchone()
            if row is None:
                return None
            hostname, country = str(row[0]), str(row[1])
            self._conn.execute(
                """
                INSERT INTO claims(hostname, station, status, updated_at, lease_until)
                VALUES (?, ?, 'claimed', ?, ?)
                ON CONFLICT(hostname) DO UPDATE SET
                    station=excluded.station,
                    status='claimed',
                    updated_at=excluded.updated_at,
                    lease_until=excluded.lease_until
                """,
                (hostname, station, ts, ts + self._lease_s),
            )
        return {"host": hostname, "country": country, "station": station}

    def record(
        self,
        hostname: str,
        station: int,
        status: str | int,
        *,
        ip: str = "",
        retry_after: float = 0.0,
        error: str = "",
        now: float | None = None,
    ) -> None:
        ts = self._clock() if now is None else float(now)
        code = str(status)
        reset_at = ts + max(0.0, float(retry_after or 0.0)) if code == "429" else 0.0
        lease_until = 0.0 if code == "ok" else ts + self._lease_s
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO claims(
                    hostname, station, status, egress_ip, retry_after,
                    reset_at, error, updated_at, lease_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hostname) DO UPDATE SET
                    station=excluded.station,
                    status=excluded.status,
                    egress_ip=excluded.egress_ip,
                    retry_after=excluded.retry_after,
                    reset_at=excluded.reset_at,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    lease_until=excluded.lease_until
                """,
                (
                    str(hostname),
                    int(station),
                    code,
                    str(ip or ""),
                    max(0.0, float(retry_after or 0.0)),
                    reset_at,
                    str(error or "")[:300],
                    ts,
                    lease_until,
                ),
            )

    def rebind_allowed(self, now: float | None = None, *, window_s: float = 60.0) -> bool:
        """Global token bucket: at most N rebinds per 60 s across all chains."""
        ts = self._clock() if now is None else float(now)
        with self._conn:
            row = self._conn.execute(
                "SELECT window_start, count FROM rebind_budget WHERE id=1"
            ).fetchone()
            if row is None or ts - float(row[0]) >= window_s:
                self._conn.execute(
                    "INSERT INTO rebind_budget(id, window_start, count) VALUES (1, ?, 1) "
                    "ON CONFLICT(id) DO UPDATE SET window_start=excluded.window_start, count=1",
                    (ts,),
                )
                return True
            if int(row[1]) >= self._rebind_limit:
                return False
            self._conn.execute(
                "UPDATE rebind_budget SET count=count+1 WHERE id=1"
            )
            return True

    def stats(self) -> dict[str, Any]:
        with self._conn:
            catalog = int(self._conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0])
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM claims GROUP BY status"
            ).fetchall()
        return {
            "catalog": catalog,
            "claims": {str(status): int(count) for status, count in rows},
        }
