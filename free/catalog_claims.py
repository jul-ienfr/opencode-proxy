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
        host_429_cooldown_s: float = 600.0,
        no_egress_backoff_s: float = 300.0,
        no_egress_backoff_max_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = str(path)
        self._lease_s = max(1.0, float(lease_s))
        self._rebind_limit = max(1, int(rebind_limit_per_min))
        self._host_429_cooldown_s = max(5.0, float(host_429_cooldown_s))
        self._no_egress_backoff_s = max(5.0, float(no_egress_backoff_s))
        self._no_egress_backoff_max_s = max(
            self._no_egress_backoff_s, float(no_egress_backoff_max_s)
        )
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
            -- NordLynx allows one live session for a shared key. A global
            -- lease serializes WireGuard so a station's handshake cannot kick
            -- the station that is already serving on it.
            CREATE TABLE IF NOT EXISTS transport_leases (
                transport TEXT PRIMARY KEY,
                station INTEGER NOT NULL,
                hostname TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()
        try:
            cols = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(claims)")
            }
            if "fail_count" not in cols:
                with self._conn:
                    self._conn.execute(
                        "ALTER TABLE claims ADD COLUMN fail_count "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def seed(self, candidates: Iterable[dict], *, prune: bool = False) -> int:
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
            if prune:
                # A previous run may have seeded hosts that the provider has
                # since removed.  Keeping them makes claim_next return invalid
                # hostnames forever, so a full filtered seed also prunes.
                self._conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS seed_hosts("
                    "hostname TEXT PRIMARY KEY)"
                )
                self._conn.execute("DELETE FROM seed_hosts")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO seed_hosts(hostname) VALUES (?)",
                    [(row[0],) for row in rows],
                )
                self._conn.execute(
                    "DELETE FROM catalog WHERE hostname NOT IN "
                    "(SELECT hostname FROM seed_hosts)"
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
                   OR (cl.status IN ('no_egress','bind_error','duplicate','abandoned','country_backoff')
                       AND cl.lease_until <= ?)
                   OR (cl.status='ok' AND cl.lease_until <= ?)
                   OR (cl.status='429' AND cl.reset_at <= ?)
                ORDER BY COALESCE(cl.fail_count, 0), c.priority, c.hostname
                LIMIT 1
                """,
                (ts, ts, ts, ts),
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

    def renew(
        self,
        hostname: str,
        station: int,
        ttl_s: float | None = None,
        now: float | None = None,
    ) -> bool:
        """Extend an active claim while its bind/probe is still running.

        A bind can legitimately last longer than the initial lease; without a
        renewal another chain could reclaim the hostname mid-bind.
        """

        ts = self._clock() if now is None else float(now)
        ttl = max(1.0, float(ttl_s if ttl_s is not None else self._lease_s))
        with self._conn:
            cur = self._conn.execute(
                "UPDATE claims SET lease_until=?, updated_at=? "
                "WHERE hostname=? AND station=? AND status='claimed'",
                (ts + ttl, ts, str(hostname), int(station)),
            )
        return int(cur.rowcount or 0) > 0

    def acquire_transport(
        self,
        transport: str,
        station: int,
        *,
        hostname: str = "",
        ttl_s: float = 360.0,
        now: float | None = None,
    ) -> bool:
        """Take the global lease for a transport shared by all stations.

        Only WireGuard currently needs this: the operator requires one shared
        NordLynx key, and a second handshake evicts the first session. A
        station already holding the lease can renew it; another station gets
        ``False`` and must try OpenVPN instead.
        """

        ts = self._clock() if now is None else float(now)
        ttl = max(30.0, float(ttl_s))
        with self._conn:
            row = self._conn.execute(
                "SELECT station, expires_at FROM transport_leases "
                "WHERE transport=?",
                (str(transport),),
            ).fetchone()
            if row is not None and int(row[0]) != int(station):
                if float(row[1]) > ts:
                    return False
            self._conn.execute(
                "INSERT INTO transport_leases(transport, station, hostname, "
                "updated_at, expires_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(transport) DO UPDATE SET station=excluded.station, "
                "hostname=excluded.hostname, updated_at=excluded.updated_at, "
                "expires_at=excluded.expires_at",
                (str(transport), int(station), str(hostname), ts, ts + ttl),
            )
        return True

    def renew_transport(
        self,
        transport: str,
        station: int,
        *,
        hostname: str = "",
        ttl_s: float = 3600.0,
        now: float | None = None,
    ) -> bool:
        ts = self._clock() if now is None else float(now)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE transport_leases SET hostname=?, updated_at=?, "
                "expires_at=? WHERE transport=? AND station=?",
                (
                    str(hostname),
                    ts,
                    ts + max(30.0, float(ttl_s)),
                    str(transport),
                    int(station),
                ),
            )
        return int(cur.rowcount or 0) > 0

    def release_transport(
        self, transport: str, station: int, *, now: float | None = None
    ) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM transport_leases WHERE transport=? AND station=?",
                (str(transport), int(station)),
            )
        return int(cur.rowcount or 0) > 0

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
        retry_after_s = max(0.0, float(retry_after or 0.0))
        # [phase-1] A 429 is an IP quota fact, not a hostname death sentence.
        # The daily ledger keeps the IP until its reset; the shared catalog
        # only needs a short host cooldown, because reconnecting the same
        # hostname can yield a different NAT egress with fresh quota.
        host_429_reset = 0.0
        if code == "429":
            host_cooldown = (
                min(retry_after_s, self._host_429_cooldown_s)
                if retry_after_s > 0
                else self._host_429_cooldown_s
            )
            host_429_reset = ts + max(5.0, host_cooldown)
        # An "ok" host stays reserved briefly for its winner, then becomes
        # reusable: keeping it locked forever removed the best servers from the
        # pool as soon as they had produced a single 200.
        lease_until = ts + self._lease_s
        fail_count = 0
        try:
            row = self._conn.execute(
                "SELECT fail_count FROM claims WHERE hostname=?", (str(hostname),)
            ).fetchone()
            fail_count = int(row[0] or 0) if row else 0
        except Exception:
            fail_count = 0
        if code in ("no_egress", "bind_error"):
            fail_count += 1
            backoff = min(
                self._no_egress_backoff_max_s,
                self._no_egress_backoff_s * (2 ** max(0, fail_count - 1)),
            )
            lease_until = ts + max(self._lease_s, backoff)
        elif code == "country_backoff":
            # Search heuristic: the whole country is skipped for 30 min. Keep
            # the host out of the claim loop for the same window instead of
            # re-claiming it every 5 min and inflating the abandoned count.
            lease_until = ts + max(self._lease_s, 1800.0)
        elif code in ("ok", "429", "duplicate", "abandoned"):
            # Not a host fault: reset the host backoff so a later real failure
            # starts again from the base delay.
            fail_count = 0
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO claims(
                    hostname, station, status, egress_ip, retry_after,
                    reset_at, error, updated_at, lease_until, fail_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hostname) DO UPDATE SET
                    station=excluded.station,
                    status=excluded.status,
                    egress_ip=excluded.egress_ip,
                    retry_after=excluded.retry_after,
                    reset_at=excluded.reset_at,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    lease_until=excluded.lease_until,
                    fail_count=excluded.fail_count
                """,
                (
                    str(hostname),
                    int(station),
                    code,
                    str(ip or ""),
                    retry_after_s,
                    host_429_reset,
                    str(error or "")[:300],
                    ts,
                    lease_until,
                    fail_count,
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
