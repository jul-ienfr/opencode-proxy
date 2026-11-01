"""SQLite implementation of the IP registry.

One connection guarded by an RLock, explicit short transactions, WAL mode.
All decisions are persisted before the network action; a crash leaves either
an expired lease or a consistent assignment, never a silent half-state.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace

from free.quota_ledger import next_utc_midnight

from .domain import (
    GeoFilter,
    IpRecord,
    IpState,
    Lease,
    ProbeResult,
    RuntimeResult,
    Server,
)
from .scoring import ScoreWeights, score_record
from .sqlite import ensure_schema

_ELIGIBLE_STATES = ("fresh", "candidate", "exhausted", "error")


class SqliteIpRegistry:
    """Persistent IP registry backed by SQLite."""

    def __init__(
        self,
        path: str,
        *,
        error_cooldown_s: float = 1800.0,
        min_score: float | None = None,
        weights: ScoreWeights | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._error_cooldown_s = max(0.0, float(error_cooldown_s))
        self._weights = weights or ScoreWeights()
        self._min_score = (
            float(min_score) if min_score is not None else float(self._weights.min_score)
        )
        self._conn: sqlite3.Connection = ensure_schema(path)
        self._conn.isolation_level = None
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except Exception:
            pass

    # ── catalog ───────────────────────────────────────────────────────────

    def sync_catalog(self, servers: Iterable[Server], now: float | None = None) -> int:
        ts = now if now is not None else self._clock()
        count = 0
        with self._lock:
            try:
                self._begin()
                for server in servers:
                    self._conn.execute(
                        """
                        INSERT INTO servers(
                            hostname, server_ip, country, city, load,
                            technologies, first_seen_at, last_seen_at, active
                        ) VALUES(?,?,?,?,?,?,?,?,1)
                        ON CONFLICT(hostname) DO UPDATE SET
                            server_ip=excluded.server_ip,
                            country=excluded.country,
                            city=excluded.city,
                            load=excluded.load,
                            technologies=excluded.technologies,
                            last_seen_at=excluded.last_seen_at,
                            active=1
                        """,
                        (
                            server.hostname,
                            server.server_ip,
                            server.country,
                            server.city,
                            server.load,
                            repr(list(server.technologies)),
                            ts,
                            ts,
                        ),
                    )
                    count += 1
                self._commit()
            except Exception:
                self._rollback()
                raise
        return count

    # ── records ───────────────────────────────────────────────────────────

    def upsert_record(self, record: IpRecord, now: float | None = None) -> IpRecord:
        ts = now if now is not None else self._clock()
        score = score_record(record, ts, weights=self._weights)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO ip_state(
                    egress_ip, hostname, country, state, score, last_status,
                    last_tested_at, last_ok_at, reset_at, error_until, latency_ms,
                    tests, successes, failures, assigned_station, assigned_at,
                    lease_id, lease_expires_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(egress_ip) DO UPDATE SET
                    hostname=excluded.hostname,
                    country=excluded.country,
                    state=excluded.state,
                    score=excluded.score,
                    last_status=excluded.last_status,
                    last_tested_at=excluded.last_tested_at,
                    last_ok_at=excluded.last_ok_at,
                    reset_at=excluded.reset_at,
                    error_until=excluded.error_until,
                    latency_ms=excluded.latency_ms,
                    tests=excluded.tests,
                    successes=excluded.successes,
                    failures=excluded.failures,
                    assigned_station=excluded.assigned_station,
                    assigned_at=excluded.assigned_at,
                    lease_id=excluded.lease_id,
                    lease_expires_at=excluded.lease_expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    record.egress_ip,
                    record.hostname,
                    record.country,
                    record.state.value,
                    score,
                    record.last_status,
                    record.last_tested_at,
                    record.last_ok_at,
                    record.reset_at,
                    record.error_until,
                    record.latency_ms,
                    record.tests,
                    record.successes,
                    record.failures,
                    record.assigned_station,
                    record.assigned_at,
                    record.lease_id,
                    record.lease_expires_at,
                    ts,
                ),
            )
            self._conn.commit()
        return self.get(record.egress_ip) or record

    def get(self, egress_ip: str) -> IpRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ip_state WHERE egress_ip=?", (egress_ip,)
            ).fetchone()
        return self._record_from_row(row) if row else None

    def list_records(
        self,
        *,
        state: IpState | None = None,
        country: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[IpRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state=?")
            params.append(state.value)
        if country:
            clauses.append("country=?")
            params.append(country)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM ip_state {where} "
            "ORDER BY score DESC, latency_ms ASC, last_tested_at DESC "
            "LIMIT ? OFFSET ?"
        )
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def eligible(
        self,
        *,
        geo: GeoFilter | None = None,
        now: float | None = None,
        limit: int = 50,
    ) -> list[IpRecord]:
        ts = now if now is not None else self._clock()
        placeholders = ",".join("?" for _ in _ELIGIBLE_STATES)
        clauses = [
            f"state IN ({placeholders})",
            "(reset_at IS NULL OR reset_at <= ?)",
            "(error_until IS NULL OR error_until <= ?)",
            "assigned_station IS NULL",
            "(lease_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)",
        ]
        params: list[object] = [*_ELIGIBLE_STATES, ts, ts, ts]
        if geo is not None and geo.require and geo.countries:
            countries = sorted(geo.countries)
            clauses.append(f"country IN ({','.join('?' for _ in countries)})")
            params.extend(countries)
        sql = (
            "SELECT * FROM ip_state WHERE "
            + " AND ".join(clauses)
            + " ORDER BY score DESC, latency_ms ASC, last_tested_at DESC"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        scored: list[tuple[float, IpRecord]] = []
        for row in rows:
            record = self._record_from_row(row)
            value = score_record(record, ts, weights=self._weights)
            if value >= self._min_score:
                scored.append((value, record))
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].latency_ms if item[1].latency_ms is not None else 10**9,
                -(item[1].last_tested_at or 0.0),
            )
        )
        return [record for _score, record in scored[: max(1, int(limit))]]

    # ── claim / record / assign / release ─────────────────────────────────

    def claim(
        self,
        egress_ip: str,
        station: int,
        ttl_s: float,
        now: float | None = None,
    ) -> Lease | None:
        ts = now if now is not None else self._clock()
        ttl = max(1.0, float(ttl_s))
        with self._lock:
            try:
                self._begin()
                row = self._conn.execute(
                    "SELECT * FROM ip_state WHERE egress_ip=?", (egress_ip,)
                ).fetchone()
                if row is None:
                    self._rollback()
                    return None
                record = self._record_from_row(row)
                if not self._claimable(record, ts):
                    self._rollback()
                    return None
                lease_id = uuid.uuid4().hex
                expires = ts + ttl
                self._conn.execute(
                    """
                    UPDATE ip_state
                    SET state='claimed', lease_id=?, lease_expires_at=?,
                        assigned_station=NULL, assigned_at=NULL, updated_at=?
                    WHERE egress_ip=?
                    """,
                    (lease_id, expires, ts, egress_ip),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise
        return Lease(
            lease_id=lease_id,
            egress_ip=record.egress_ip,
            hostname=record.hostname,
            station=int(station),
            expires_at=expires,
        )

    def record(self, lease: Lease, result: ProbeResult) -> None:
        ts = result.tested_at
        with self._lock:
            try:
                self._begin()
                row = self._conn.execute(
                    "SELECT * FROM ip_state WHERE egress_ip=?", (lease.egress_ip,)
                ).fetchone()
                if row is None or str(row["lease_id"] or "") != lease.lease_id:
                    self._rollback()
                    raise ValueError("lease does not match the registry state")
                record = self._record_from_row(row)
                updated = self._apply_result(record, result)
                score = score_record(updated, ts, weights=self._weights)
                self._conn.execute(
                    """
                    UPDATE ip_state SET
                        state=?, score=?, last_status=?, last_tested_at=?, last_ok_at=?,
                        reset_at=?, error_until=?, latency_ms=?, tests=?, successes=?,
                        failures=?, updated_at=?
                    WHERE egress_ip=? AND lease_id=?
                    """,
                    (
                        updated.state.value,
                        score,
                        updated.last_status,
                        updated.last_tested_at,
                        updated.last_ok_at,
                        updated.reset_at,
                        updated.error_until,
                        updated.latency_ms,
                        updated.tests,
                        updated.successes,
                        updated.failures,
                        ts,
                        lease.egress_ip,
                        lease.lease_id,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO observations(
                        hostname, egress_ip, station, tested_at, status,
                        retry_after_s, reset_at, latency_ms, error, attempt_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.hostname,
                        lease.egress_ip,
                        lease.station,
                        ts,
                        int(result.status),
                        max(0.0, float(result.retry_after_s)),
                        updated.reset_at,
                        updated.latency_ms,
                        result.error,
                        lease.lease_id,
                    ),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def assign(self, lease: Lease, now: float | None = None) -> IpRecord | None:
        ts = now if now is not None else self._clock()
        with self._lock:
            try:
                self._begin()
                row = self._conn.execute(
                    "SELECT * FROM ip_state WHERE egress_ip=?", (lease.egress_ip,)
                ).fetchone()
                if row is None or str(row["lease_id"] or "") != lease.lease_id:
                    self._rollback()
                    return None
                record = self._record_from_row(row)
                if record.last_status != 200 or record.state is not IpState.CLAIMED:
                    self._rollback()
                    return None
                self._conn.execute(
                    """
                    UPDATE ip_state
                    SET state='assigned', assigned_station=?, assigned_at=?,
                        lease_id=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE egress_ip=? AND lease_id=?
                    """,
                    (lease.station, ts, ts, lease.egress_ip, lease.lease_id),
                )
                self._conn.execute(
                    """
                    INSERT INTO assignments(
                        egress_ip, hostname, station, assigned_at, result
                    ) VALUES(?,?,?,?,?)
                    """,
                    (lease.egress_ip, lease.hostname, lease.station, ts, "200"),
                )
                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                return None
            except Exception:
                self._rollback()
                raise
        return self.get(lease.egress_ip)

    def release(
        self,
        lease: Lease,
        reason: str,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else self._clock()
        with self._lock:
            try:
                self._begin()
                row = self._conn.execute(
                    "SELECT * FROM ip_state WHERE egress_ip=?", (lease.egress_ip,)
                ).fetchone()
                if row is None:
                    self._rollback()
                    return
                record = self._record_from_row(row)
                next_state, error_until = self._released_state(record, ts, reason)
                self._conn.execute(
                    """
                    UPDATE ip_state SET
                        state=?, error_until=?, assigned_station=NULL, assigned_at=NULL,
                        lease_id=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE egress_ip=?
                    """,
                    (next_state.value, error_until, ts, lease.egress_ip),
                )
                self._conn.execute(
                    """
                    UPDATE assignments
                    SET released_at=?, reason=?, result=?
                    WHERE egress_ip=? AND station=? AND released_at IS NULL
                    """,
                    (
                        ts,
                        reason,
                        str(record.last_status or ""),
                        lease.egress_ip,
                        lease.station,
                    ),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def record_runtime(self, result: RuntimeResult) -> None:
        """Persist a 429/error observed while an assigned IP is serving."""
        ts = result.observed_at
        with self._lock:
            try:
                self._begin()
                row = self._conn.execute(
                    "SELECT * FROM ip_state WHERE egress_ip=?", (result.egress_ip,)
                ).fetchone()
                if row is None:
                    self._rollback()
                    return
                record = self._record_from_row(row)
                obs_reset_at: float | None = None
                if result.status == 429:
                    retry_after = max(0.0, float(result.retry_after_s))
                    reset_at = (
                        ts + retry_after if retry_after > 0 else next_utc_midnight(ts)
                    )
                    if record.reset_at is not None:
                        reset_at = max(reset_at, float(record.reset_at))
                    obs_reset_at = reset_at
                    self._conn.execute(
                        """
                        UPDATE ip_state SET
                            state='exhausted', last_status=429, last_tested_at=?,
                            reset_at=?, assigned_station=NULL, assigned_at=NULL,
                            lease_id=NULL, lease_expires_at=NULL, updated_at=?
                        WHERE egress_ip=?
                        """,
                        (ts, reset_at, ts, result.egress_ip),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE ip_state SET
                            state='error', last_status=?, last_tested_at=?,
                            error_until=?, assigned_station=NULL, assigned_at=NULL,
                            lease_id=NULL, lease_expires_at=NULL, updated_at=?
                        WHERE egress_ip=?
                        """,
                        (
                            int(result.status),
                            ts,
                            ts + self._error_cooldown_s,
                            ts,
                            result.egress_ip,
                        ),
                    )
                self._conn.execute(
                    """
                    UPDATE assignments
                    SET released_at=?, reason='runtime_429', result=?
                    WHERE egress_ip=? AND station=? AND released_at IS NULL
                    """,
                    (ts, str(result.status), result.egress_ip, result.station),
                )
                self._conn.execute(
                    """
                    INSERT INTO observations(
                        hostname, egress_ip, station, tested_at, status,
                        retry_after_s, reset_at, latency_ms, error, attempt_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        result.hostname or record.hostname,
                        result.egress_ip,
                        result.station,
                        ts,
                        int(result.status),
                        max(0.0, float(result.retry_after_s)),
                        obs_reset_at,
                        None,
                        result.error_type,
                        result.request_id,
                    ),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def reconcile(self, now: float | None = None) -> dict[str, int]:
        ts = now if now is not None else self._clock()
        released_leases = 0
        closed_assignments = 0
        with self._lock:
            try:
                self._begin()
                rows = self._conn.execute(
                    """
                    SELECT egress_ip FROM ip_state
                    WHERE state='claimed' AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    """,
                    (ts,),
                ).fetchall()
                for row in rows:
                    self._conn.execute(
                        """
                        UPDATE ip_state SET
                            state='candidate', lease_id=NULL, lease_expires_at=NULL,
                            updated_at=?
                        WHERE egress_ip=? AND state='claimed'
                        """,
                        (ts, row["egress_ip"]),
                    )
                    released_leases += 1
                orphan_rows = self._conn.execute(
                    """
                    SELECT a.id FROM assignments a
                    JOIN ip_state s ON s.egress_ip = a.egress_ip
                    WHERE a.released_at IS NULL
                      AND (s.assigned_station IS NULL OR s.assigned_station != a.station)
                    """
                ).fetchall()
                for row in orphan_rows:
                    self._conn.execute(
                        "UPDATE assignments SET released_at=?, reason='reconcile' WHERE id=?",
                        (ts, row["id"]),
                    )
                    closed_assignments += 1
                self._commit()
            except Exception:
                self._rollback()
                raise
        return {
            "released_leases": released_leases,
            "closed_assignments": closed_assignments,
        }

    # ── helpers ───────────────────────────────────────────────────────────

    def _claimable(self, record: IpRecord, now: float) -> bool:
        if record.state not in (
            IpState.FRESH,
            IpState.CANDIDATE,
            IpState.EXHAUSTED,
            IpState.ERROR,
        ):
            return False
        if record.assigned_station is not None:
            return False
        if record.is_leased(now):
            return False
        if record.is_exhausted(now):
            return False
        if record.is_in_error_cooldown(now):
            return False
        return score_record(record, now, weights=self._weights) >= self._min_score

    def _apply_result(self, record: IpRecord, result: ProbeResult) -> IpRecord:
        tests = record.tests + 1
        successes = record.successes + (1 if result.ok else 0)
        failures = record.failures + (0 if result.ok else 1)
        latency = result.latency_ms if result.latency_ms is not None else record.latency_ms
        common = {
            "tests": tests,
            "successes": successes,
            "failures": failures,
            "last_status": int(result.status),
            "last_tested_at": result.tested_at,
            "latency_ms": latency,
        }
        if result.ok:
            return replace(
                record,
                **common,
                last_ok_at=result.tested_at,
                reset_at=None,
                error_until=None,
            )
        if result.status == 429:
            retry_after = max(0.0, float(result.retry_after_s))
            reset_at = (
                result.tested_at + retry_after
                if retry_after > 0
                else next_utc_midnight(result.tested_at)
            )
            if record.reset_at is not None:
                reset_at = max(reset_at, float(record.reset_at))
            return replace(
                record,
                **common,
                state=IpState.EXHAUSTED,
                reset_at=reset_at,
            )
        return replace(
            record,
            **common,
            state=IpState.ERROR,
            error_until=result.tested_at + self._error_cooldown_s,
        )

    def _released_state(
        self, record: IpRecord, now: float, reason: str
    ) -> tuple[IpState, float | None]:
        # A bind/egress failure proves nothing about the quota, but it does
        # prove the server is not usable right now: short error cooldown,
        # never an immediate retry of the same dead server.
        if record.last_status is None and reason.startswith(("bind_", "egress_")):
            until = record.error_until
            if until is None or until <= now:
                until = now + self._error_cooldown_s
            return IpState.ERROR, until
        if record.last_status == 429 or (record.reset_at is not None and now < record.reset_at):
            return IpState.EXHAUSTED, record.error_until
        if record.error_until is not None and now < record.error_until:
            return IpState.ERROR, record.error_until
        return IpState.CANDIDATE, record.error_until

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> IpRecord:
        return IpRecord(
            egress_ip=str(row["egress_ip"]),
            hostname=str(row["hostname"]),
            country=str(row["country"]),
            state=IpState(str(row["state"])),
            score=float(row["score"] or 0.0),
            last_status=int(row["last_status"]) if row["last_status"] is not None else None,
            last_tested_at=float(row["last_tested_at"]) if row["last_tested_at"] is not None else None,
            last_ok_at=float(row["last_ok_at"]) if row["last_ok_at"] is not None else None,
            reset_at=float(row["reset_at"]) if row["reset_at"] is not None else None,
            error_until=float(row["error_until"]) if row["error_until"] is not None else None,
            latency_ms=float(row["latency_ms"]) if row["latency_ms"] is not None else None,
            tests=int(row["tests"] or 0),
            successes=int(row["successes"] or 0),
            failures=int(row["failures"] or 0),
            assigned_station=(
                int(row["assigned_station"]) if row["assigned_station"] is not None else None
            ),
            assigned_at=float(row["assigned_at"]) if row["assigned_at"] is not None else None,
            lease_id=str(row["lease_id"]) if row["lease_id"] is not None else None,
            lease_expires_at=(
                float(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
        )
