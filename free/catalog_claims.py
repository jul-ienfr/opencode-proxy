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

    def claim_next(
        self,
        station: int,
        now: float | None = None,
        *,
        forbidden_ips: set[str] | None = None,
    ) -> dict[str, Any] | None:
        ts = self._clock() if now is None else float(now)
        station = int(station)
        with self._conn:
            # An expired lease is reclaimable.
            self._conn.execute(
                "UPDATE claims SET status='abandoned' "
                "WHERE status='claimed' AND lease_until < ?",
                (ts,),
            )
            row = self._qualification_candidate(
                ts,
                evidence_only=True,
                forbidden_ips=forbidden_ips,
            )
            if row is None:
                row = self._legacy_candidate(ts, forbidden_ips=forbidden_ips)
            if row is None:
                row = self._qualification_candidate(
                    ts,
                    forbidden_ips=forbidden_ips,
                )
            if row is None:
                return None
            hostname, country = str(row[0]), str(row[1])
            self._mark_qualification_claimed(hostname, ts)
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

    def _qualification_candidate(
        self,
        ts: float,
        *,
        evidence_only: bool = False,
        forbidden_ips: set[str] | None = None,
    ) -> tuple[str, str] | None:
        """Best candidate from the feature-014 queue.

        ``evidence_only`` limits the queue to hostnames that already proved a
        200 (or a quota reset): an unmeasured row must not preempt the legacy
        catalog's known-200 priority.  The unmeasured backlog stays available
        to the qualification sweep and is used only when the catalog is dry.

        ``forbidden_ips`` drops candidates whose last observed egress IP is
        already exhausted in the ledger.  A hostname can be re-linked to the
        same NAT IP; binding it again only burns the rebind budget.

        Falls back silently to the legacy ``catalog`` table so old databases
        and tests keep working during the strangler migration.
        """

        evidence = (
            "AND (q.success_count > 0 OR q.measured_score > 0 "
            "OR q.status = 'quota')"
            if evidence_only
            else ""
        )
        blocked = sorted(
            {
                str(ip).strip()
                for ip in (forbidden_ips or set())
                if str(ip).strip()
            }
        )
        ip_clause = ""
        claim_ip_clause = ""
        ip_params: list[str] = []
        if blocked:
            placeholders = ",".join("?" for _ in blocked)
            ip_clause = (
                "AND (q.last_egress_ip IS NULL OR q.last_egress_ip = '' "
                f"OR q.last_egress_ip NOT IN ({placeholders}))"
            )
            # [P0 2026-11-10] Le claim est la source la plus récente : une
            # ligne de qualification legacy peut avoir last_egress_ip vide
            # alors que claims.egress_ip porte l'IP brûlée (uk2433/al73).
            claim_ip_clause = (
                "AND (cl.egress_ip IS NULL OR cl.egress_ip = '' "
                f"OR cl.egress_ip NOT IN ({placeholders}))"
            )
            ip_params = blocked
        try:
            row = self._conn.execute(
                f"""
                SELECT q.hostname, q.country
                FROM qualification_candidates q
                LEFT JOIN claims cl ON cl.hostname = q.hostname
                WHERE q.status IN ('pending','quota','claimed')
                  AND q.next_eligible_at <= ?
                  AND (q.quota_reset_at IS NULL OR q.quota_reset_at <= ?)
                  AND q.transports <> '[]'
                  {evidence}
                  {ip_clause}
                  {claim_ip_clause}
                  AND (
                    cl.hostname IS NULL
                    OR (cl.status='claimed' AND cl.lease_until < ?)
                    OR (cl.status IN ('no_egress','bind_error','duplicate','abandoned','country_backoff')
                        AND cl.lease_until <= ?)
                    OR (cl.status='ok' AND cl.lease_until <= ?)
                    OR (cl.status='429' AND cl.reset_at <= ?)
                  )
                ORDER BY
                  CASE WHEN q.success_count > 0 THEN 0
                       WHEN q.status = 'quota' THEN 1
                       ELSE 2 END,
                  q.success_count DESC,
                  q.measured_score DESC,
                  q.potential_score DESC,
                  COALESCE(q.quota_remaining, 0) DESC,
                  COALESCE(q.rtt_ms, 1e9) ASC,
                  q.load ASC,
                  q.hostname ASC
                LIMIT 1
                """,
                (ts, ts, *ip_params, *ip_params, ts, ts, ts, ts),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return (str(row[0]), str(row[1])) if row is not None else None

    def _legacy_candidate(
        self,
        ts: float,
        *,
        forbidden_ips: set[str] | None = None,
    ) -> tuple[str, str] | None:
        # [P0 2026-11-10] Le catalogue legacy n'avait aucune notion d'egress :
        # un hostname dont la dernière IP est épuisée était re-pioché en boucle
        # (al73/al74/uk2433) et re-consommait un bind pour un 429 garanti. Le
        # claim porte l'egress_ip : on l'utilise comme filtre, exactement comme
        # la file de qualification.
        blocked = sorted(
            {
                str(ip).strip()
                for ip in (forbidden_ips or set())
                if str(ip).strip()
            }
        )
        ip_clause = ""
        ip_params: list[str] = []
        if blocked:
            placeholders = ",".join("?" for _ in blocked)
            ip_clause = (
                "AND (cl.egress_ip IS NULL OR cl.egress_ip = '' "
                f"OR cl.egress_ip NOT IN ({placeholders}))"
            )
            ip_params = blocked
        row = self._conn.execute(
            f"""
            SELECT c.hostname, c.country
            FROM catalog c
            LEFT JOIN claims cl ON cl.hostname = c.hostname
            WHERE (cl.hostname IS NULL
               OR (cl.status='claimed' AND cl.lease_until < ?)
               OR (cl.status IN ('no_egress','bind_error','duplicate','abandoned','country_backoff')
                   AND cl.lease_until <= ?)
               OR (cl.status='ok' AND cl.lease_until <= ?)
               OR (cl.status='429' AND cl.reset_at <= ?))
              {ip_clause}
            ORDER BY c.priority, COALESCE(cl.fail_count, 0), c.hostname
            LIMIT 1
            """,
            (ts, ts, ts, ts, *ip_params),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row is not None else None

    def _mark_qualification_claimed(self, hostname: str, ts: float) -> None:
        try:
            self._conn.execute(
                "UPDATE qualification_candidates SET status='claimed', updated_at=? WHERE hostname=?",
                (ts, str(hostname)),
            )
        except sqlite3.OperationalError:
            pass

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
            self._sync_qualification_status(
                hostname,
                code,
                ts=ts,
                ip=str(ip or ""),
                lease_until=lease_until,
                host_429_reset=host_429_reset,
            )

    def _sync_qualification_status(
        self,
        hostname: str,
        code: str,
        *,
        ts: float,
        ip: str,
        lease_until: float,
        host_429_reset: float,
    ) -> None:
        """Keep ``qualification_candidates`` in step with the claim outcome.

        Without this, ``_mark_qualification_claimed`` left every claimed
        hostname stuck in ``claimed`` forever: the repair queue silently
        drained into a status no claim query reads.  A failed bind now returns
        the host to ``pending`` with its backoff, a 429 returns it to ``quota``
        with the reset, and a real 200 promotes it to ``usable``.
        """

        try:
            if code in ("ok", "200"):
                self._conn.execute(
                    """
                    UPDATE qualification_candidates
                    SET status='usable', success_count=success_count+1,
                        fail_count=0, last_result='200', last_egress_ip=?,
                        next_eligible_at=0, quota_reset_at=NULL, updated_at=?
                    WHERE hostname=?
                    """,
                    (ip, ts, str(hostname)),
                )
            elif code == "429":
                self._conn.execute(
                    """
                    UPDATE qualification_candidates
                    SET status='quota', fail_count=fail_count+1,
                        last_result='429', next_eligible_at=?,
                        quota_reset_at=?, updated_at=?
                    WHERE hostname=?
                    """,
                    (host_429_reset, host_429_reset, ts, str(hostname)),
                )
            elif code in (
                "no_egress",
                "bind_error",
                "duplicate",
                "abandoned",
                "timeout",
            ):
                self._conn.execute(
                    """
                    UPDATE qualification_candidates
                    SET status='pending', infra_failures=infra_failures+1,
                        last_result=?, next_eligible_at=?, updated_at=?
                    WHERE hostname=?
                    """,
                    (code, lease_until, ts, str(hostname)),
                )
            elif code in ("auth", "dead"):
                self._conn.execute(
                    """
                    UPDATE qualification_candidates
                    SET status=?, last_result=?, updated_at=?
                    WHERE hostname=?
                    """,
                    (code, code, ts, str(hostname)),
                )
        except sqlite3.OperationalError:
            # The qualification table is optional during the strangler
            # migration; legacy databases must keep working.
            pass

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
