"""SQLite repositories for the target registry.

The repository is intentionally small and explicit: no ORM, no hidden
transactions. Use :class:`UnitOfWork` for a write transaction.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .migrate import connect


class UnitOfWork(AbstractContextManager["UnitOfWork"]):
    def __init__(self, path: str | Path, *, now: float | None = None) -> None:
        self._path = path
        self._conn = connect(path)
        self.repo = RegistryRepository(self._conn)
        self._now = time.time() if now is None else float(now)

    def __enter__(self) -> UnitOfWork:
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


class RegistryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── catalog ────────────────────────────────────────────────────────
    def upsert_source(
        self,
        source_id: str,
        kind: str,
        *,
        fetched_at: float | None = None,
        stale: bool = False,
        server_count: int = 0,
        error: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO catalog_sources(source_id, kind, fetched_at, stale, "
            "server_count, error) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind, "
            "fetched_at=excluded.fetched_at, stale=excluded.stale, "
            "server_count=excluded.server_count, error=excluded.error",
            (str(source_id), str(kind), fetched_at, 1 if stale else 0,
             int(server_count), str(error)),
        )

    def upsert_server(
        self,
        *,
        hostname: str,
        country: str,
        source_id: str,
        city: str = "",
        region: str = "",
        load: float = 0.0,
        status: str = "",
        technologies: list[str] | None = None,
        endpoint_ip: str = "",
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        host = str(hostname).strip()
        if not host:
            raise ValueError("hostname is required")
        self._conn.execute(
            "INSERT INTO servers(hostname, country, city, region, load, status, "
            "source_id, first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(hostname) DO UPDATE SET country=excluded.country, "
            "city=excluded.city, region=excluded.region, load=excluded.load, "
            "status=excluded.status, source_id=excluded.source_id, "
            "last_seen_at=excluded.last_seen_at",
            (host, str(country or "unknown"), str(city), str(region),
             float(load), str(status), str(source_id), ts, ts),
        )
        for transport in technologies or []:
            self._conn.execute(
                "INSERT INTO server_transports(hostname, transport, supported) "
                "VALUES(?,?,1) ON CONFLICT(hostname, transport) DO UPDATE SET "
                "supported=1",
                (host, str(transport)),
            )
        if endpoint_ip:
            for transport in technologies or []:
                self._conn.execute(
                    "INSERT OR IGNORE INTO endpoints(hostname, transport, "
                    "endpoint_ip, source, first_seen_at, last_seen_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (host, transport, str(endpoint_ip), str(source_id), ts, ts),
                )

    # ── IPs ────────────────────────────────────────────────────────────
    def upsert_ip(
        self,
        *,
        ip: str,
        country: str = "",
        state: str = "fresh",
        score: float = 0.0,
        successes: int = 0,
        failures: int = 0,
        latency_ms: float = 0.0,
        reset_at: float = 0.0,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        self._conn.execute(
            "INSERT INTO egress_ips(ip, country, first_seen_at, last_seen_at, "
            "successes, failures, latency_ms, reset_at, score_ip, state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET country=excluded.country, "
            "last_seen_at=excluded.last_seen_at, successes=excluded.successes, "
            "failures=excluded.failures, latency_ms=excluded.latency_ms, "
            "reset_at=excluded.reset_at, score_ip=excluded.score_ip, "
            "state=excluded.state",
            (str(ip), str(country), ts, ts, int(successes), int(failures),
             float(latency_ms), float(reset_at or 0.0) or None,
             float(score), str(state)),
        )

    def get_ip(self, ip: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM egress_ips WHERE ip=?", (str(ip),)
        ).fetchone()
        return dict(row) if row else None

    def available_ips(
        self,
        *,
        now: float | None = None,
        limit: int = 50,
        exclude: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        ts = time.time() if now is None else float(now)
        excluded = set(exclude or ())
        rows = self._conn.execute(
            "SELECT e.* FROM egress_ips e "
            "LEFT JOIN leases l ON l.ip=e.ip AND l.status='active' "
            "WHERE e.state IN ('fresh','candidate') "
            "AND (e.reset_at IS NULL OR e.reset_at <= ?) "
            "AND (e.last_429_at IS NULL OR e.last_429_at < ?) "
            "AND l.lease_id IS NULL "
            "ORDER BY e.score_ip DESC, e.latency_ms ASC LIMIT ?",
            (ts, ts - 300.0, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows if str(row["ip"]) not in excluded]

    # ── observations / scores ──────────────────────────────────────────
    def record_observation(
        self,
        *,
        hostname: str,
        transport: str,
        status: int,
        station: int = 0,
        ip: str = "",
        model: str = "",
        valid: bool = True,
        retry_after_s: float = 0.0,
        reset_at: float = 0.0,
        latency_ms: float = 0.0,
        error: str = "",
        tokens_out: int = 0,
        tested_at: float | None = None,
    ) -> None:
        ts = time.time() if tested_at is None else float(tested_at)
        self._conn.execute(
            "INSERT INTO observations(station, hostname, transport, ip, model, "
            "status, valid, retry_after_s, reset_at, latency_ms, tokens_out, "
            "error, tested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(station), str(hostname), str(transport), str(ip), str(model),
             int(status), 1 if valid else 0, float(retry_after_s),
             float(reset_at or 0.0) or None, float(latency_ms),
             int(tokens_out), str(error), ts),
        )

    def compute_scores(
        self,
        *,
        now: float | None = None,
        window_days: float = 7.0,
    ) -> int:
        ts = time.time() if now is None else float(now)
        cutoff = ts - max(1.0, float(window_days)) * 86400.0
        written = 0
        for kind, key in (("ip", "ip"), ("host", "hostname")):
            rows = self._conn.execute(
                f"SELECT {key} AS key, transport, "
                "SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok, "
                "COUNT(*) n, "
                "AVG(CASE WHEN status=200 THEN latency_ms END) lat, "
                "AVG(tokens_out) tps "
                f"FROM observations WHERE tested_at >= ? AND {key} IS NOT NULL "
                f"AND {key} != '' GROUP BY {key}, transport",
                (cutoff,),
            ).fetchall()
            for row in rows:
                n = int(row["n"] or 0)
                ok = int(row["ok"] or 0)
                rate = (ok + 1.0) / (n + 2.0) if n else 0.0
                lat = float(row["lat"] or 0.0)
                latency_score = 1.0 - min(1.0, lat / 8000.0) if lat else 0.5
                score = 0.65 * rate + 0.25 * latency_score + 0.10
                self._conn.execute(
                    "INSERT OR REPLACE INTO score_snapshots("
                    "kind, key, transport, score, success_rate, latency_p50, "
                    "throughput_tps, stability, freshness, samples, computed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (kind, str(row["key"]), str(row["transport"] or ""),
                     score, rate, lat, float(row["tps"] or 0.0), 0.5, 0.5,
                     n, ts),
                )
                written += 1
        return written

    # ── leases / warm pool ─────────────────────────────────────────────
    def claim_lease(
        self,
        *,
        station: int,
        ip: str,
        hostname: str,
        transport: str,
        ttl_s: float = 1800.0,
        model: str = "",
        now: float | None = None,
    ) -> str | None:
        ts = time.time() if now is None else float(now)
        lease_id = uuid.uuid4().hex
        try:
            self._conn.execute(
                "INSERT INTO leases(lease_id, station, ip, hostname, transport, "
                "model, started_at, renewed_at, expires_at, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,'active')",
                (lease_id, int(station), str(ip), str(hostname), str(transport),
                 str(model), ts, ts, ts + max(1.0, float(ttl_s))),
            )
        except sqlite3.IntegrityError:
            return None
        self._conn.execute(
            "UPDATE egress_ips SET state='assigned', last_seen_at=? WHERE ip=?",
            (ts, str(ip)),
        )
        return lease_id

    def release_lease(
        self,
        lease_id: str,
        *,
        reason: str = "released",
        now: float | None = None,
    ) -> bool:
        ts = time.time() if now is None else float(now)
        row = self._conn.execute(
            "SELECT ip FROM leases WHERE lease_id=? AND status='active'",
            (str(lease_id),),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute(
            "UPDATE leases SET status='released', released_at=?, reason=? "
            "WHERE lease_id=?",
            (ts, str(reason), str(lease_id)),
        )
        self._conn.execute(
            "UPDATE egress_ips SET state='candidate', last_seen_at=? WHERE ip=?",
            (ts, str(row["ip"])),
        )
        return True

    def active_lease_for_station(self, station: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM leases WHERE station=? AND status='active' LIMIT 1",
            (int(station),),
        ).fetchone()
        return dict(row) if row else None

    def put_warm_spare(
        self,
        *,
        ip: str,
        hostname: str,
        transport: str,
        model: str = "",
        ttl_s: float = 21600.0,
        score: float = 0.0,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        self._conn.execute(
            "INSERT OR REPLACE INTO warm_pool(ip, hostname, transport, model, "
            "verified_at, expires_at, score_ip) VALUES(?,?,?,?,?,?,?)",
            (str(ip), str(hostname), str(transport), str(model), ts,
             ts + max(60.0, float(ttl_s)), float(score)),
        )

    # ── sweep cursor ───────────────────────────────────────────────────
    def record_sweep(
        self,
        *,
        batch_id: str,
        country: str,
        hostname: str,
        transport: str,
        status: str,
        next_eligible_at: float,
        tested_at: float | None = None,
        failures: int = 0,
    ) -> None:
        ts = time.time() if tested_at is None else float(tested_at)
        with self._conn:
            self._conn.execute(
                "INSERT INTO sweep_cursor(batch_id, country, hostname, transport, "
                "last_tested_at, next_eligible_at, attempts, failures, last_status) "
                "VALUES(?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(batch_id, country, hostname, transport) DO UPDATE SET "
                "last_tested_at=excluded.last_tested_at, "
                "next_eligible_at=excluded.next_eligible_at, "
                "attempts=sweep_cursor.attempts+1, "
                "failures=sweep_cursor.failures+excluded.failures, "
                "last_status=excluded.last_status",
                (
                    str(batch_id),
                    str(country),
                    str(hostname),
                    str(transport),
                    ts,
                    float(next_eligible_at),
                    int(failures),
                    str(status),
                ),
            )

    def next_sweep(
        self, batch_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        ts = time.time() if now is None else float(now)
        row = self._conn.execute(
            "SELECT * FROM sweep_cursor WHERE batch_id=? AND next_eligible_at <= ? "
            "ORDER BY next_eligible_at, id LIMIT 1",
            (str(batch_id), ts),
        ).fetchone()
        return dict(row) if row else None

    def sweep_progress(self, batch_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT last_status, COUNT(*) AS n FROM sweep_cursor "
            "WHERE batch_id=? GROUP BY last_status",
            (str(batch_id),),
        ).fetchall()
        out = {str(row["last_status"] or "unknown"): int(row["n"]) for row in rows}
        out["total"] = sum(out.values())
        return out

    # ── warm pool ──────────────────────────────────────────────────────
    def take_warm_spare(
        self,
        *,
        transport: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        ts = time.time() if now is None else float(now)
        sql = (
            "SELECT * FROM warm_pool WHERE expires_at > ? "
            "AND (? IS NULL OR transport = ?) "
            "ORDER BY score_ip DESC, verified_at DESC LIMIT 1"
        )
        with self._conn:
            row = self._conn.execute(
                sql, (ts, transport, transport)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM warm_pool WHERE ip=?", (str(row["ip"]),)
            )
        return dict(row)

    def count_warm_spares(self, *, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM warm_pool WHERE expires_at > ?", (ts,)
        ).fetchone()
        return int(row["n"] if row else 0)

    def expire_warm_spares(self, *, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM warm_pool WHERE expires_at <= ?", (ts,)
            )
        return int(cur.rowcount or 0)

    # ── repair queue ───────────────────────────────────────────────────
    def request_repair(
        self,
        *,
        station: int,
        reason: str,
        model: str = "",
        ip: str = "",
        hostname: str = "",
        ttl_s: float = 1800.0,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        self._conn.execute(
            "INSERT INTO repair_requests(station, reason, model, ip, hostname, "
            "requested_at, expires_at, status) VALUES(?,?,?,?,?,?,?,'pending') "
            "ON CONFLICT(station) DO UPDATE SET reason=excluded.reason, "
            "model=excluded.model, ip=excluded.ip, hostname=excluded.hostname, "
            "requested_at=excluded.requested_at, expires_at=excluded.expires_at, "
            "status='pending', claimed_at=NULL",
            (int(station), str(reason), str(model), str(ip), str(hostname),
             ts, ts + max(30.0, float(ttl_s))),
        )

    def pending_repairs(self, *, now: float | None = None) -> list[dict[str, Any]]:
        ts = time.time() if now is None else float(now)
        rows = self._conn.execute(
            "SELECT * FROM repair_requests WHERE expires_at > ? "
            "AND status IN ('pending','claimed') ORDER BY requested_at",
            (ts,),
        ).fetchall()
        return [dict(row) for row in rows]

    def complete_repair(self, station: int) -> None:
        self._conn.execute(
            "DELETE FROM repair_requests WHERE station=?", (int(station),)
        )
