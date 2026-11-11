"""SQLite store for the NordVPN qualification queue.

The store owns only the ``qualification_*`` tables inside ``registry.db``.
It never rewrites historical egress/observation rows.  Every mutation is
transactional; the queue is reconstructible from the catalog and the attempts.

Constitution III: ``registry.db`` is the authority.  No JSON state file is a
second source of truth.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .domain import Attempt, AttemptStatus, Candidate, CandidateStatus
from .epochs import EpochStoreMixin
from .operator import set_paused_row
from .repair_store import last_repair_at as last_repair_at_row
from .scoring import measured_score
from .transport_health import refresh_transport_health

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qualification_candidates (
    hostname            TEXT PRIMARY KEY,
    provider            TEXT NOT NULL DEFAULT 'nordvpn',
    country             TEXT NOT NULL DEFAULT '',
    city                TEXT NOT NULL DEFAULT '',
    load                REAL NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
    transports          TEXT NOT NULL DEFAULT '[]',
    transport_cursor    TEXT NOT NULL DEFAULT '',
    transport_round     INTEGER NOT NULL DEFAULT 0,
    potential_score     REAL NOT NULL DEFAULT 0,
    measured_score      REAL NOT NULL DEFAULT 0,
    rtt_ms              REAL,
    quota_remaining     INTEGER,
    quota_reset_at      REAL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    infra_failures      INTEGER NOT NULL DEFAULT 0,
    success_count       INTEGER NOT NULL DEFAULT 0,
    fail_count          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     REAL,
    last_result         TEXT NOT NULL DEFAULT '',
    last_egress_ip      TEXT NOT NULL DEFAULT '',
    last_transport      TEXT NOT NULL DEFAULT '',
    next_eligible_at    REAL NOT NULL DEFAULT 0,
    epoch_id            TEXT NOT NULL DEFAULT '',
    lease_until         REAL NOT NULL DEFAULT 0,
    skipped_reason      TEXT NOT NULL DEFAULT '',
    source_id           TEXT NOT NULL DEFAULT '',
    first_seen_at       REAL NOT NULL,
    last_seen_at        REAL NOT NULL,
    updated_at          REAL NOT NULL,
    meta_json           TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_qual_order
    ON qualification_candidates(status, next_eligible_at, potential_score DESC);
CREATE INDEX IF NOT EXISTS idx_qual_country
    ON qualification_candidates(country, status);
CREATE INDEX IF NOT EXISTS idx_qual_epoch
    ON qualification_candidates(epoch_id, status);

CREATE TABLE IF NOT EXISTS qualification_attempts (
    attempt_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL DEFAULT '',
    epoch_id        TEXT NOT NULL DEFAULT '',
    station         INTEGER NOT NULL DEFAULT 0,
    hostname        TEXT NOT NULL,
    transport       TEXT NOT NULL DEFAULT '',
    round           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    egress_ip       TEXT NOT NULL DEFAULT '',
    latency_ms      REAL NOT NULL DEFAULT 0,
    rtt_ms          REAL NOT NULL DEFAULT 0,
    dns_ms          REAL NOT NULL DEFAULT 0,
    connect_ms      REAL NOT NULL DEFAULT 0,
    handshake_ms    REAL NOT NULL DEFAULT 0,
    endpoint_ip     TEXT NOT NULL DEFAULT '',
    reset_at        REAL NOT NULL DEFAULT 0,
    retry_after_s   REAL NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    validation      TEXT NOT NULL DEFAULT '',
    started_at      REAL NOT NULL,
    finished_at     REAL NOT NULL,
    provider        TEXT NOT NULL DEFAULT 'nordvpn'
);

CREATE INDEX IF NOT EXISTS idx_attempt_host
    ON qualification_attempts(hostname, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_epoch
    ON qualification_attempts(epoch_id, status);
CREATE INDEX IF NOT EXISTS idx_attempt_station
    ON qualification_attempts(station, started_at DESC);

CREATE TABLE IF NOT EXISTS qualification_epochs (
    epoch_id        TEXT PRIMARY KEY,
    provider        TEXT NOT NULL DEFAULT 'nordvpn',
    status          TEXT NOT NULL DEFAULT 'running',
    total           INTEGER NOT NULL DEFAULT 0,
    tested          INTEGER NOT NULL DEFAULT 0,
    usable          INTEGER NOT NULL DEFAULT 0,
    quota           INTEGER NOT NULL DEFAULT 0,
    infra           INTEGER NOT NULL DEFAULT 0,
    dead            INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    cursor          TEXT NOT NULL DEFAULT '',
    started_at      REAL NOT NULL,
    finished_at     REAL,
    reason          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_epoch_status
    ON qualification_epochs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS server_transports (
    hostname    TEXT NOT NULL,
    transport   TEXT NOT NULL,
    supported   INTEGER NOT NULL DEFAULT 1,
    wg_pubkey   TEXT,
    last_ok_at  REAL,
    last_error  TEXT,
    PRIMARY KEY (hostname, transport)
);

CREATE INDEX IF NOT EXISTS idx_transport_supported
    ON server_transports(supported);

CREATE TABLE IF NOT EXISTS transport_health (
    transport       TEXT NOT NULL,
    station         INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    successes       INTEGER NOT NULL DEFAULT 0,
    quota           INTEGER NOT NULL DEFAULT 0,
    infra           INTEGER NOT NULL DEFAULT 0,
    latency_p50_ms  REAL,
    success_rate    REAL NOT NULL DEFAULT 0,
    degraded        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL,
    PRIMARY KEY (transport, station)
);

CREATE INDEX IF NOT EXISTS idx_transport_health_degraded
    ON transport_health(degraded, transport);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT '',
    started_at          REAL NOT NULL,
    finished_at         REAL,
    status              TEXT NOT NULL DEFAULT 'running',
    server_count        INTEGER NOT NULL DEFAULT 0,
    transport_count     INTEGER NOT NULL DEFAULT 0,
    stale               INTEGER NOT NULL DEFAULT 0,
    catalog_fetched_at  REAL,
    error               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_discovery_started
    ON discovery_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS station_state (
    station             INTEGER PRIMARY KEY,
    container           TEXT NOT NULL DEFAULT '',
    transport_effective TEXT NOT NULL DEFAULT '',
    state               TEXT NOT NULL DEFAULT '',
    country             TEXT NOT NULL DEFAULT '',
    current_ip          TEXT NOT NULL DEFAULT '',
    current_host        TEXT NOT NULL DEFAULT '',
    client_usable       INTEGER NOT NULL DEFAULT 0,
    last_probe_at       REAL NOT NULL DEFAULT 0,
    last_200_at         REAL,
    reason              TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL DEFAULT 'station',
    station     INTEGER,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    cause       TEXT NOT NULL DEFAULT '',
    remediation TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON incidents(ended_at, station, cause);

CREATE TABLE IF NOT EXISTS repair_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    station          INTEGER NOT NULL,
    cause            TEXT NOT NULL DEFAULT '',
    started_at       REAL NOT NULL,
    finished_at      REAL,
    candidates_tried INTEGER NOT NULL DEFAULT 0,
    transports_tried TEXT NOT NULL DEFAULT '',
    result           TEXT NOT NULL DEFAULT 'running',
    last_error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_repair_jobs_station
    ON repair_jobs(station, started_at DESC);

CREATE TABLE IF NOT EXISTS station_cutovers (
    cutover_id       TEXT PRIMARY KEY,
    station          INTEGER NOT NULL,
    from_option_key  TEXT NOT NULL DEFAULT '',
    from_egress_ip   TEXT NOT NULL DEFAULT '',
    to_option_key    TEXT NOT NULL DEFAULT '',
    to_egress_ip     TEXT NOT NULL DEFAULT '',
    transport        TEXT NOT NULL DEFAULT '',
    started_at       REAL NOT NULL,
    committed_at     REAL,
    finished_at      REAL,
    duration_ms      REAL,
    result           TEXT NOT NULL DEFAULT 'pending',
    reason           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cutover_station
    ON station_cutovers(station, started_at DESC);

CREATE TABLE IF NOT EXISTS station_runtime (
    station             INTEGER PRIMARY KEY,
    state               TEXT NOT NULL DEFAULT 'idle',
    current_option_key  TEXT NOT NULL DEFAULT '',
    current_egress_ip   TEXT NOT NULL DEFAULT '',
    current_transport   TEXT NOT NULL DEFAULT '',
    standby_option_key  TEXT NOT NULL DEFAULT '',
    standby_egress_ip   TEXT NOT NULL DEFAULT '',
    standby_transport   TEXT NOT NULL DEFAULT '',
    lease_until         REAL NOT NULL DEFAULT 0,
    last_cutover_at     REAL,
    cutover_ms          REAL,
    availability_1h     REAL NOT NULL DEFAULT 0,
    availability_24h    REAL NOT NULL DEFAULT 0,
    last_reason         TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watchdog_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at REAL NOT NULL DEFAULT 0,
    last_run_at  REAL,
    status       TEXT NOT NULL DEFAULT 'idle',
    error        TEXT NOT NULL DEFAULT ''
);
"""


def _candidate_from_row(row: sqlite3.Row) -> Candidate:
    try:
        transports = tuple(json.loads(row["transports"] or "[]"))
    except Exception:
        transports = ()
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except Exception:
        meta = {}
    return Candidate(
        hostname=str(row["hostname"]),
        provider=str(row["provider"]),
        country=str(row["country"] or ""),
        city=str(row["city"] or ""),
        load=float(row["load"] or 0.0),
        status=CandidateStatus(str(row["status"] or "pending")),
        transports=transports,
        transport_cursor=str(row["transport_cursor"] or ""),
        transport_round=int(row["transport_round"] or 0),
        potential_score=float(row["potential_score"] or 0.0),
        measured_score=float(row["measured_score"] or 0.0),
        rtt_ms=float(row["rtt_ms"]) if row["rtt_ms"] is not None else None,
        quota_remaining=int(row["quota_remaining"]) if row["quota_remaining"] is not None else None,
        quota_reset_at=float(row["quota_reset_at"]) if row["quota_reset_at"] is not None else None,
        attempts=int(row["attempts"] or 0),
        infra_failures=int(row["infra_failures"] or 0),
        success_count=int(row["success_count"] or 0),
        fail_count=int(row["fail_count"] or 0),
        last_attempt_at=float(row["last_attempt_at"]) if row["last_attempt_at"] is not None else None,
        last_result=str(row["last_result"] or ""),
        last_egress_ip=str(row["last_egress_ip"] or ""),
        last_transport=str(row["last_transport"] or ""),
        next_eligible_at=float(row["next_eligible_at"] or 0.0),
        epoch_id=str(row["epoch_id"] or ""),
        skipped_reason=str(row["skipped_reason"] or ""),
        source_id=str(row["source_id"] or ""),
        first_seen_at=float(row["first_seen_at"] or 0.0),
        last_seen_at=float(row["last_seen_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
        meta=meta,
    )


class QualificationStore(EpochStoreMixin):
    """Transactional queue/attempt/epoch store."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        retention_days: float = 7.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(db_path)
        self.retention_days = max(1.0, float(retention_days))
        self._clock = clock
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA_SQL)
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(qualification_candidates)")
        }
        if "lease_until" not in columns:
            self._conn.execute(
                "ALTER TABLE qualification_candidates "
                "ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"
            )
        attempt_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(qualification_attempts)")
        }
        for name, ddl in (
            ("dns_ms", "REAL NOT NULL DEFAULT 0"),
            ("connect_ms", "REAL NOT NULL DEFAULT 0"),
            ("handshake_ms", "REAL NOT NULL DEFAULT 0"),
            ("endpoint_ip", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in attempt_columns:
                self._conn.execute(
                    f"ALTER TABLE qualification_attempts ADD COLUMN {name} {ddl}"
                )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── ingestion ─────────────────────────────────────────────────────────

    def upsert_candidates(self, candidates: Iterable[Candidate], *, now: float | None = None) -> int:
        ts = float(self._clock() if now is None else now)
        rows = list(candidates)
        if not rows:
            return 0
        with self._lock, self._conn:
            for candidate in rows:
                hostname = str(candidate.hostname or "").strip()
                if not hostname:
                    continue
                existing = self._conn.execute(
                    "SELECT status, skipped_reason FROM qualification_candidates WHERE hostname=?",
                    (hostname,),
                ).fetchone()
                status = candidate.status
                skipped_reason = candidate.skipped_reason
                if existing is not None:
                    old_status = str(existing["status"] or "pending")
                    old_reason = str(existing["skipped_reason"] or "")
                    # A host that reappears after a catalog removal returns to
                    # pending; a host that lost its transports is skipped.
                    if candidate.status is CandidateStatus.SKIPPED and candidate.skipped_reason:
                        status = CandidateStatus.SKIPPED
                        skipped_reason = candidate.skipped_reason
                    elif old_status == CandidateStatus.SKIPPED and old_reason in (
                        "removed-from-catalog",
                        "unsupported-transport",
                        "dedicated",
                        "denylisted",
                    ) and candidate.status is CandidateStatus.PENDING:
                        status = CandidateStatus.PENDING
                        skipped_reason = ""
                    else:
                        status = CandidateStatus(old_status)
                        skipped_reason = old_reason
                self._conn.execute(
                    """
                    INSERT INTO qualification_candidates(
                        hostname, provider, country, city, load, status, transports,
                        transport_cursor, transport_round, potential_score, measured_score,
                        rtt_ms, quota_remaining, quota_reset_at, attempts, infra_failures,
                        success_count, fail_count, last_attempt_at, last_result,
                        last_egress_ip, last_transport, next_eligible_at, epoch_id,
                        skipped_reason, source_id, first_seen_at, last_seen_at, updated_at, meta_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(hostname) DO UPDATE SET
                        provider=excluded.provider,
                        country=excluded.country,
                        city=excluded.city,
                        load=excluded.load,
                        transports=excluded.transports,
                        potential_score=excluded.potential_score,
                        rtt_ms=COALESCE(excluded.rtt_ms, qualification_candidates.rtt_ms),
                        quota_remaining=COALESCE(excluded.quota_remaining, qualification_candidates.quota_remaining),
                        quota_reset_at=COALESCE(excluded.quota_reset_at, qualification_candidates.quota_reset_at),
                        status=CASE
                            WHEN excluded.status='skipped' THEN excluded.status
                            WHEN qualification_candidates.status='skipped'
                             AND qualification_candidates.skipped_reason IN (
                                 'removed-from-catalog','unsupported-transport',
                                 'dedicated','denylisted')
                             AND excluded.status='pending' THEN excluded.status
                            ELSE qualification_candidates.status
                        END,
                        skipped_reason=CASE
                            WHEN excluded.status='skipped' THEN excluded.skipped_reason
                            WHEN qualification_candidates.status='skipped'
                             AND qualification_candidates.skipped_reason IN (
                                 'removed-from-catalog','unsupported-transport',
                                 'dedicated','denylisted')
                             AND excluded.status='pending' THEN ''
                            ELSE qualification_candidates.skipped_reason
                        END,
                        source_id=excluded.source_id,
                        last_seen_at=excluded.last_seen_at,
                        updated_at=excluded.updated_at,
                        meta_json=excluded.meta_json
                    """,
                    (
                        hostname,
                        str(candidate.provider or "nordvpn"),
                        str(candidate.country or ""),
                        str(candidate.city or ""),
                        float(candidate.load or 0.0),
                        str(status),
                        json.dumps(list(candidate.transports), ensure_ascii=False),
                        str(candidate.transport_cursor or ""),
                        int(candidate.transport_round or 0),
                        float(candidate.potential_score or 0.0),
                        float(candidate.measured_score or 0.0),
                        candidate.rtt_ms,
                        candidate.quota_remaining,
                        candidate.quota_reset_at,
                        int(candidate.attempts or 0),
                        int(candidate.infra_failures or 0),
                        int(candidate.success_count or 0),
                        int(candidate.fail_count or 0),
                        candidate.last_attempt_at,
                        str(candidate.last_result or ""),
                        str(candidate.last_egress_ip or ""),
                        str(candidate.last_transport or ""),
                        float(candidate.next_eligible_at or 0.0),
                        str(candidate.epoch_id or ""),
                        str(skipped_reason or ""),
                        str(candidate.source_id or ""),
                        float(candidate.first_seen_at or ts),
                        float(candidate.last_seen_at or ts),
                        float(candidate.updated_at or ts),
                        json.dumps(dict(candidate.meta or {}), ensure_ascii=False),
                    ),
                )
        return len(rows)

    def upsert_server_transports(self, candidates: Iterable[Candidate]) -> int:
        rows = [
            (str(candidate.hostname), str(transport))
            for candidate in candidates
            for transport in candidate.transports
            if candidate.hostname and transport
        ]
        if not rows:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO server_transports(hostname, transport, supported)
                VALUES(?, ?, 1)
                ON CONFLICT(hostname, transport) DO UPDATE SET supported=1
                """,
                rows,
            )
        return len(rows)

    def get(self, hostname: str) -> Candidate | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM qualification_candidates WHERE hostname=?",
                (str(hostname),),
            ).fetchone()
        return _candidate_from_row(row) if row is not None else None

    def top_candidates(self, limit: int = 20) -> list[Candidate]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM qualification_candidates
                WHERE status IN ('pending','quota')
                ORDER BY
                  CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                  potential_score DESC,
                  COALESCE(quota_remaining, 0) DESC,
                  COALESCE(rtt_ms, 1e9) ASC,
                  load ASC,
                  hostname ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def candidates_for_preflight(
        self,
        *,
        limit: int = 500,
        transport: str = "wireguard",
    ) -> list[Candidate]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM qualification_candidates
                WHERE status IN ('pending','quota')
                  AND rtt_ms IS NULL
                  AND instr(transports, ?) > 0
                ORDER BY potential_score DESC, load ASC, hostname ASC
                LIMIT ?
                """,
                (f'"{str(transport)}"', max(1, int(limit))),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def set_preflight(
        self,
        hostname: str,
        *,
        rtt_ms: float,
        dns_ms: float = 0.0,
        connect_ms: float = 0.0,
        handshake_ms: float = 0.0,
        endpoint_ip: str = "",
        potential_score: float | None = None,
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        candidate = self.get(hostname)
        if candidate is None:
            return
        meta = dict(candidate.meta or {})
        meta.update(
            {
                "dns_ms": float(dns_ms or 0.0),
                "connect_ms": float(connect_ms or 0.0),
                "handshake_ms": float(handshake_ms or 0.0),
                "endpoint_ip": str(endpoint_ip or ""),
                "preflight_at": ts,
            }
        )
        score = float(potential_score) if potential_score is not None else float(candidate.potential_score or 0.0)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE qualification_candidates
                SET rtt_ms=?, potential_score=?, meta_json=?, updated_at=?
                WHERE hostname=?
                """,
                (float(rtt_ms), score, json.dumps(meta, ensure_ascii=False), ts, str(hostname)),
            )

    def set_preflight_error(
        self,
        hostname: str,
        *,
        error: str,
        next_eligible_at: float = 0.0,
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE qualification_candidates
                SET last_result=?, next_eligible_at=?, updated_at=?
                WHERE hostname=?
                """,
                (f"preflight:{str(error)[:300]}", float(next_eligible_at or 0.0), ts, str(hostname)),
            )

    def mark_missing_hosts(
        self,
        seen: Iterable[str],
        *,
        reason: str = "removed-from-catalog",
        now: float | None = None,
    ) -> int:
        ts = float(self._clock() if now is None else now)
        keep = {str(host).strip().lower() for host in seen if str(host).strip()}
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT hostname, status FROM qualification_candidates"
            ).fetchall()
            missing = [
                str(row["hostname"])
                for row in rows
                if str(row["hostname"]).lower() not in keep
                and str(row["status"]) not in ("skipped", "dead")
            ]
            for start in range(0, len(missing), 500):
                chunk = missing[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                self._conn.execute(
                    f"""
                    UPDATE qualification_candidates
                    SET status='skipped', skipped_reason=?, updated_at=?
                    WHERE hostname IN ({placeholders})
                    """,
                    (str(reason), ts, *chunk),
                )
        return len(missing)

    def record_discovery_run(
        self,
        *,
        source: str,
        started_at: float,
        finished_at: float | None = None,
        status: str = "ok",
        server_count: int = 0,
        transport_count: int = 0,
        stale: bool = False,
        catalog_fetched_at: float | None = None,
        error: str = "",
    ) -> str:
        run_id = f"disc_{uuid.uuid4().hex}"
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO discovery_runs(
                    id, source, started_at, finished_at, status, server_count,
                    transport_count, stale, catalog_fetched_at, error
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    str(source or ""),
                    float(started_at),
                    float(finished_at) if finished_at is not None else None,
                    str(status),
                    int(server_count or 0),
                    int(transport_count or 0),
                    1 if stale else 0,
                    float(catalog_fetched_at) if catalog_fetched_at is not None else None,
                    str(error or "")[:500],
                ),
            )
        return run_id

    def last_discovery_run(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM discovery_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "source": str(row["source"] or ""),
            "started_at": float(row["started_at"] or 0.0),
            "finished_at": float(row["finished_at"]) if row["finished_at"] is not None else None,
            "status": str(row["status"] or ""),
            "server_count": int(row["server_count"] or 0),
            "transport_count": int(row["transport_count"] or 0),
            "stale": bool(row["stale"]),
            "catalog_fetched_at": float(row["catalog_fetched_at"]) if row["catalog_fetched_at"] is not None else None,
            "error": str(row["error"] or ""),
        }

    # ── station state / watchdog / repair ────────────────────────────────

    def upsert_station_state(
        self,
        station: int,
        *,
        state: str,
        container: str = "",
        transport: str = "",
        country: str = "",
        current_ip: str = "",
        current_host: str = "",
        client_usable: bool = False,
        last_probe_at: float | None = None,
        last_200_at: float | None = None,
        reason: str = "",
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO station_state(
                    station, container, transport_effective, state, country,
                    current_ip, current_host, client_usable, last_probe_at,
                    last_200_at, reason, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(station) DO UPDATE SET
                    container=excluded.container,
                    transport_effective=excluded.transport_effective,
                    state=excluded.state,
                    country=excluded.country,
                    current_ip=excluded.current_ip,
                    current_host=excluded.current_host,
                    client_usable=excluded.client_usable,
                    last_probe_at=excluded.last_probe_at,
                    last_200_at=COALESCE(excluded.last_200_at, station_state.last_200_at),
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    int(station),
                    str(container or ""),
                    str(transport or ""),
                    str(state or ""),
                    str(country or ""),
                    str(current_ip or ""),
                    str(current_host or ""),
                    1 if client_usable else 0,
                    float(last_probe_at if last_probe_at is not None else ts),
                    float(last_200_at) if last_200_at is not None else None,
                    str(reason or "")[:500],
                    ts,
                ),
            )

    def station_state(self, station: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM station_state WHERE station=?",
                (int(station),),
            ).fetchone()
        if row is None:
            return None
        return {
            "station": int(row["station"]),
            "container": str(row["container"] or ""),
            "transport": str(row["transport_effective"] or ""),
            "state": str(row["state"] or ""),
            "country": str(row["country"] or ""),
            "current_ip": str(row["current_ip"] or ""),
            "current_host": str(row["current_host"] or ""),
            "client_usable": bool(row["client_usable"]),
            "last_probe_at": float(row["last_probe_at"] or 0.0),
            "last_200_at": float(row["last_200_at"]) if row["last_200_at"] is not None else None,
            "reason": str(row["reason"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def list_station_states(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT station FROM station_state ORDER BY station"
            ).fetchall()
        return [state for row in rows if (state := self.station_state(int(row["station"]))) is not None]

    def open_incident(
        self,
        station: int,
        cause: str,
        *,
        details: str = "",
        remediation: str = "",
        scope: str = "station",
        now: float | None = None,
    ) -> int:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT id FROM incidents
                WHERE station=? AND cause=? AND ended_at IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (int(station), str(cause)),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            cur = self._conn.execute(
                """
                INSERT INTO incidents(scope, station, started_at, cause, remediation, details)
                VALUES(?,?,?,?,?,?)
                """,
                (str(scope), int(station), ts, str(cause), str(remediation or ""), str(details or "")[:1000]),
            )
        return int(cur.lastrowid or 0)

    def close_incident(
        self,
        incident_id: int,
        *,
        remediation: str = "",
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE incidents SET ended_at=?, remediation=COALESCE(NULLIF(?,''), remediation)
                WHERE id=? AND ended_at IS NULL
                """,
                (ts, str(remediation or ""), int(incident_id)),
            )

    def close_station_incidents(
        self,
        station: int,
        *,
        remediation: str = "auto-cleared",
        now: float | None = None,
    ) -> int:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE incidents SET ended_at=?, remediation=?
                WHERE station=? AND ended_at IS NULL
                """,
                (ts, str(remediation or "auto-cleared"), int(station)),
            )
        return int(cur.rowcount or 0)

    def open_incidents(self, *, station: int | None = None) -> list[dict[str, Any]]:
        clauses = ["ended_at IS NULL"]
        values: list[Any] = []
        if station is not None:
            clauses.append("station=?")
            values.append(int(station))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM incidents WHERE {' AND '.join(clauses)} ORDER BY id",
                tuple(values),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "scope": str(row["scope"] or ""),
                "station": int(row["station"]) if row["station"] is not None else None,
                "started_at": float(row["started_at"] or 0.0),
                "cause": str(row["cause"] or ""),
                "remediation": str(row["remediation"] or ""),
                "details": str(row["details"] or ""),
            }
            for row in rows
        ]

    def record_repair_job(
        self,
        station: int,
        cause: str,
        *,
        result: str = "running",
        transports_tried: str = "",
        last_error: str = "",
        now: float | None = None,
    ) -> int:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO repair_jobs(station, cause, started_at, result, transports_tried, last_error)
                VALUES(?,?,?,?,?,?)
                """,
                (int(station), str(cause), ts, str(result), str(transports_tried or ""), str(last_error or "")[:500]),
            )
        return int(cur.lastrowid or 0)

    def finish_repair_job(
        self,
        job_id: int,
        *,
        result: str,
        last_error: str = "",
        transports_tried: str = "",
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE repair_jobs
                SET finished_at=?, result=?, last_error=?, transports_tried=COALESCE(NULLIF(?,''), transports_tried)
                WHERE id=?
                """,
                (ts, str(result), str(last_error or "")[:500], str(transports_tried or ""), int(job_id)),
            )

    def repair_attempts(self, station: int, cause: str, *, since: float = 0.0) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) FROM repair_jobs
                WHERE station=? AND cause=? AND started_at>=?
                """,
                (int(station), str(cause), float(since)),
            ).fetchone()
        return int(row[0] if row else 0)

    def last_repair_at(self, station: int, cause: str) -> float | None:
        with self._lock:
            return last_repair_at_row(self._conn, station, cause)

    def record_cutover(
        self,
        *,
        station: int,
        from_option_key: str = "",
        from_egress_ip: str = "",
        to_option_key: str = "",
        to_egress_ip: str = "",
        transport: str = "",
        started_at: float,
        committed_at: float | None = None,
        finished_at: float | None = None,
        duration_ms: float | None = None,
        result: str = "pending",
        reason: str = "",
    ) -> str:
        cutover_id = f"cut_{uuid.uuid4().hex}"
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO station_cutovers(
                    cutover_id, station, from_option_key, from_egress_ip,
                    to_option_key, to_egress_ip, transport, started_at,
                    committed_at, finished_at, duration_ms, result, reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cutover_id,
                    int(station),
                    str(from_option_key or ""),
                    str(from_egress_ip or ""),
                    str(to_option_key or ""),
                    str(to_egress_ip or ""),
                    str(transport or ""),
                    float(started_at),
                    float(committed_at) if committed_at is not None else None,
                    float(finished_at) if finished_at is not None else None,
                    float(duration_ms) if duration_ms is not None else None,
                    str(result or "pending"),
                    str(reason or "")[:500],
                ),
            )
        return cutover_id

    def upsert_station_runtime(
        self,
        station: int,
        *,
        state: str = "idle",
        current_option_key: str = "",
        current_egress_ip: str = "",
        current_transport: str = "",
        standby_option_key: str = "",
        standby_egress_ip: str = "",
        standby_transport: str = "",
        lease_until: float = 0.0,
        last_cutover_at: float | None = None,
        cutover_ms: float | None = None,
        availability_1h: float = 0.0,
        availability_24h: float = 0.0,
        last_reason: str = "",
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO station_runtime(
                    station, state, current_option_key, current_egress_ip,
                    current_transport, standby_option_key, standby_egress_ip,
                    standby_transport, lease_until, last_cutover_at, cutover_ms,
                    availability_1h, availability_24h, last_reason, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(station) DO UPDATE SET
                    state=excluded.state,
                    current_option_key=COALESCE(NULLIF(excluded.current_option_key,''), station_runtime.current_option_key),
                    current_egress_ip=COALESCE(NULLIF(excluded.current_egress_ip,''), station_runtime.current_egress_ip),
                    current_transport=COALESCE(NULLIF(excluded.current_transport,''), station_runtime.current_transport),
                    standby_option_key=COALESCE(NULLIF(excluded.standby_option_key,''), station_runtime.standby_option_key),
                    standby_egress_ip=COALESCE(NULLIF(excluded.standby_egress_ip,''), station_runtime.standby_egress_ip),
                    standby_transport=COALESCE(NULLIF(excluded.standby_transport,''), station_runtime.standby_transport),
                    lease_until=excluded.lease_until,
                    last_cutover_at=COALESCE(excluded.last_cutover_at, station_runtime.last_cutover_at),
                    cutover_ms=COALESCE(excluded.cutover_ms, station_runtime.cutover_ms),
                    availability_1h=excluded.availability_1h,
                    availability_24h=excluded.availability_24h,
                    last_reason=excluded.last_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    int(station),
                    str(state or "idle"),
                    str(current_option_key or ""),
                    str(current_egress_ip or ""),
                    str(current_transport or ""),
                    str(standby_option_key or ""),
                    str(standby_egress_ip or ""),
                    str(standby_transport or ""),
                    float(lease_until or 0.0),
                    float(last_cutover_at) if last_cutover_at is not None else None,
                    float(cutover_ms) if cutover_ms is not None else None,
                    float(availability_1h or 0.0),
                    float(availability_24h or 0.0),
                    str(last_reason or "")[:500],
                    ts,
                ),
            )

    def station_runtime(self, station: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM station_runtime WHERE station=?",
                (int(station),),
            ).fetchone()
        if row is None:
            return None
        return {
            "station": int(row["station"]),
            "state": str(row["state"] or "idle"),
            "current_option_key": str(row["current_option_key"] or ""),
            "current_egress_ip": str(row["current_egress_ip"] or ""),
            "current_transport": str(row["current_transport"] or ""),
            "standby_option_key": str(row["standby_option_key"] or ""),
            "standby_egress_ip": str(row["standby_egress_ip"] or ""),
            "standby_transport": str(row["standby_transport"] or ""),
            "lease_until": float(row["lease_until"] or 0.0),
            "last_cutover_at": float(row["last_cutover_at"]) if row["last_cutover_at"] is not None else None,
            "cutover_ms": float(row["cutover_ms"]) if row["cutover_ms"] is not None else None,
            "availability_1h": float(row["availability_1h"] or 0.0),
            "availability_24h": float(row["availability_24h"] or 0.0),
            "last_reason": str(row["last_reason"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def list_station_runtimes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT station FROM station_runtime ORDER BY station"
            ).fetchall()
        return [state for row in rows if (state := self.station_runtime(int(row["station"]))) is not None]

    # ── queue ─────────────────────────────────────────────────────────────

    def claim_next(
        self,
        station: int,
        *,
        now: float | None = None,
        lease_s: float = 300.0,
        epoch_id: str = "",
    ) -> Candidate | None:
        ts = float(self._clock() if now is None else now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # Crash recovery: a claim whose lease expired returns to the
                # queue instead of staying `claimed` forever.
                self._conn.execute(
                    """
                    UPDATE qualification_candidates
                    SET status='pending', lease_until=0, updated_at=?
                    WHERE status='claimed' AND lease_until <= ?
                    """,
                    (ts, ts),
                )
                row = self._conn.execute(
                    """
                    SELECT * FROM qualification_candidates
                    WHERE status IN ('pending','quota')
                      AND next_eligible_at <= ?
                      AND (quota_reset_at IS NULL OR quota_reset_at <= ?)
                      AND transports <> '[]'
                    ORDER BY
                      CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                      potential_score DESC,
                      COALESCE(quota_remaining, 0) DESC,
                      COALESCE(rtt_ms, 1e9) ASC,
                      load ASC,
                      hostname ASC
                    LIMIT 1
                    """,
                    (ts, ts),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                lease_until = ts + max(1.0, float(lease_s))
                self._conn.execute(
                    """
                    UPDATE qualification_candidates
                    SET status='claimed', epoch_id=?, lease_until=?, updated_at=?
                    WHERE hostname=?
                    """,
                    (
                        str(epoch_id or row["epoch_id"] or ""),
                        lease_until,
                        ts,
                        row["hostname"],
                    ),
                )
                claimed = self._conn.execute(
                    "SELECT * FROM qualification_candidates WHERE hostname=?",
                    (row["hostname"],),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return _candidate_from_row(claimed) if claimed is not None else None

    def egress_ip_in_use(self, egress_ip: str, *, station: int = 0) -> bool:
        """True when a usable egress option is owned by another station.

        The sweep must never promote a duplicate IP.  The query is read-only
        and fails soft when the egress catalog tables are absent.
        """

        ip = str(egress_ip or "").strip()
        if not ip:
            return False
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM egress_options
                    WHERE egress_ip=? AND status='usable' AND station<>?
                    LIMIT 1
                    """,
                    (ip, str(int(station))),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        return row is not None

    def has_claimable(self, *, now: float | None = None) -> bool:
        ts = float(self._clock() if now is None else now)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM qualification_candidates
                WHERE status IN ('pending','quota')
                  AND next_eligible_at <= ?
                  AND (quota_reset_at IS NULL OR quota_reset_at <= ?)
                  AND transports <> '[]'
                LIMIT 1
                """,
                (ts, ts),
            ).fetchone()
        return row is not None

    def set_paused(
        self,
        hostname: str,
        *,
        paused: bool = True,
        now: float | None = None,
    ) -> bool:
        """Operator pause/resume for one hostname, independent of the sweep."""

        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            return set_paused_row(
                self._conn,
                hostname,
                paused=paused,
                now=ts,
            )

    def release(
        self,
        hostname: str,
        *,
        status: CandidateStatus = CandidateStatus.PENDING,
        now: float | None = None,
        next_eligible_at: float = 0.0,
        reason: str = "",
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE qualification_candidates
                SET status=?, next_eligible_at=?, skipped_reason=?, lease_until=0,
                    updated_at=?
                WHERE hostname=?
                """,
                (str(status), float(next_eligible_at or 0.0), str(reason or ""), ts, str(hostname)),
            )

    # ── attempts ──────────────────────────────────────────────────────────

    def record_attempt(self, attempt: Attempt) -> str:
        attempt_id = f"att_{uuid.uuid4().hex}"
        ts = float(attempt.finished_at or attempt.started_at or self._clock())
        with self._lock, self._conn:
            if attempt.transport:
                duplicate = self._conn.execute(
                    """
                    SELECT 1 FROM qualification_attempts
                    WHERE hostname = ? AND transport = ? AND round = ?
                      AND epoch_id = ? AND station = ?
                    LIMIT 1
                    """,
                    (
                        str(attempt.hostname),
                        str(attempt.transport),
                        int(attempt.round or 0),
                        str(attempt.epoch_id or ""),
                        int(attempt.station or 0),
                    ),
                ).fetchone()
                if duplicate is not None:
                    return ""
            self._conn.execute(
                """
                INSERT INTO qualification_attempts(
                    attempt_id, run_id, epoch_id, station, hostname, transport, round,
                    status, egress_ip, latency_ms, rtt_ms, dns_ms, connect_ms,
                    handshake_ms, endpoint_ip, reset_at, retry_after_s,
                    error, validation, started_at, finished_at, provider
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    str(attempt.run_id or ""),
                    str(attempt.epoch_id or ""),
                    int(attempt.station or 0),
                    str(attempt.hostname),
                    str(attempt.transport or ""),
                    int(attempt.round or 0),
                    str(attempt.status),
                    str(attempt.egress_ip or ""),
                    float(attempt.latency_ms or 0.0),
                    float(attempt.rtt_ms or 0.0),
                    float(attempt.dns_ms or 0.0),
                    float(attempt.connect_ms or 0.0),
                    float(attempt.handshake_ms or 0.0),
                    str(attempt.endpoint_ip or ""),
                    float(attempt.reset_at or 0.0),
                    float(attempt.retry_after_s or 0.0),
                    str(attempt.error or "")[:500],
                    str(attempt.validation or "")[:500],
                    float(attempt.started_at or ts),
                    ts,
                    str(attempt.provider or "nordvpn"),
                ),
            )
        return attempt_id

    def record_chain_attempt(self, attempt: Attempt) -> str:
        """Tentative réelle de la chaîne → transport_health."""
        attempt_id = self.record_attempt(attempt)
        with self._lock, self._conn:
            refresh_transport_health(self._conn, attempt, min_attempts=100, now=float(attempt.finished_at or self._clock()))
        return attempt_id

    def apply_attempt(
        self,
        hostname: str,
        attempt: Attempt,
        *,
        candidate_status: CandidateStatus,
        next_eligible_at: float = 0.0,
        quota_reset_at: float | None = None,
        measured: float | None = None,
        transport_cursor: str = "",
        transport_round: int = 0,
        reason: str = "",
        transport_health_min_attempts: int = 100,
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        candidate = self.get(hostname)
        if candidate is None:
            return
        infra = attempt.status in {
            AttemptStatus.NO_EGRESS,
            AttemptStatus.BIND_ERROR,
            AttemptStatus.TIMEOUT,
            AttemptStatus.DUPLICATE,
        }
        success = 1 if attempt.status is AttemptStatus.OK else 0
        failure = 1 if attempt.status in {AttemptStatus.QUOTA, AttemptStatus.DEAD} else 0
        measured_value = float(measured) if measured is not None else float(candidate.measured_score or 0.0)
        last_egress = str(attempt.egress_ip or candidate.last_egress_ip or "")
        last_result = str(attempt.status)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE qualification_candidates SET
                    status=?, attempts=attempts+1,
                    infra_failures=infra_failures+?,
                    success_count=success_count+?,
                    fail_count=fail_count+?,
                    last_attempt_at=?, last_result=?, last_egress_ip=?,
                    last_transport=?, measured_score=?,
                    next_eligible_at=?, quota_reset_at=COALESCE(?, quota_reset_at),
                    transport_cursor=?, transport_round=?,
                    skipped_reason=?, updated_at=?
                WHERE hostname=?
                """,
                (
                    str(candidate_status),
                    1 if infra else 0,
                    success,
                    failure,
                    ts,
                    last_result,
                    last_egress,
                    str(attempt.transport or ""),
                    measured_value,
                    float(next_eligible_at or 0.0),
                    quota_reset_at,
                    str(transport_cursor or ""),
                    int(transport_round or 0),
                    str(reason or "")[:500],
                    ts,
                    str(hostname),
                ),
            )
            refresh_transport_health(
                self._conn,
                attempt,
                min_attempts=transport_health_min_attempts,
                now=ts,
            )

    def promote(
        self,
        hostname: str,
        *,
        egress_ip: str,
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        candidate = self.get(hostname)
        if candidate is None:
            return
        score = measured_score(candidate, now=ts)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE qualification_candidates
                SET status='usable', measured_score=?, last_egress_ip=?,
                    next_eligible_at=0, quota_reset_at=NULL, updated_at=?
                WHERE hostname=?
                """,
                (score, str(egress_ip or ""), ts, str(hostname)),
            )

    # ── epochs / summaries ────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        with self._lock:
            by_status: dict[CandidateStatus, int] = {}
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM qualification_candidates GROUP BY status"
            ):
                by_status[CandidateStatus(str(row["status"]))] = int(row["n"])
            total = sum(by_status.values())
            attempts = int(
                self._conn.execute("SELECT COUNT(*) FROM qualification_attempts").fetchone()[0]
            )
            tested = int(
                self._conn.execute(
                    "SELECT COUNT(DISTINCT hostname) FROM qualification_attempts"
                ).fetchone()[0]
            )
            last = self._conn.execute(
                "SELECT MAX(finished_at) FROM qualification_attempts"
            ).fetchone()[0]
            infra = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM qualification_attempts WHERE status IN ('no_egress','bind_error','timeout','duplicate')"
                ).fetchone()[0]
            )
        return {
            "total": total,
            "by_status": by_status,
            "tests": attempts,
            "tested": tested,
            "infra": infra,
            "last_attempt_at": float(last) if last is not None else None,
            "usable": by_status.get(CandidateStatus.USABLE, 0),
            "quota": by_status.get(CandidateStatus.QUOTA, 0),
            "pending": by_status.get(CandidateStatus.PENDING, 0),
            "dead": by_status.get(CandidateStatus.DEAD, 0),
            "skipped": by_status.get(CandidateStatus.SKIPPED, 0),
        }

    def touch_watchdog(
        self,
        *,
        status: str = "running",
        error: str = "",
        now: float | None = None,
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO watchdog_state(id, heartbeat_at, last_run_at, status, error)
                VALUES(1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    heartbeat_at=excluded.heartbeat_at,
                    last_run_at=excluded.last_run_at,
                    status=excluded.status,
                    error=excluded.error
                """,
                (ts, ts, str(status or "running"), str(error or "")[:500]),
            )

    def watchdog_state(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM watchdog_state WHERE id=1").fetchone()
        if row is None:
            return None
        return {
            "heartbeat_at": float(row["heartbeat_at"] or 0.0),
            "last_run_at": float(row["last_run_at"]) if row["last_run_at"] is not None else None,
            "status": str(row["status"] or ""),
            "error": str(row["error"] or ""),
        }

    def watchdog_stale(self, *, max_age_s: float = 90.0, now: float | None = None) -> bool:
        ts = float(self._clock() if now is None else now)
        state = self.watchdog_state()
        if state is None:
            return True
        return ts - float(state["heartbeat_at"] or 0.0) > max(1.0, float(max_age_s))

    def purge_attempts(self, *, now: float | None = None) -> int:
        """Purge expired qualification history and return the row count."""

        ts = float(self._clock() if now is None else now)
        cutoff = ts - (self.retention_days * 86400.0)
        targets = (
            ("qualification_attempts", "finished_at"),
            ("station_cutovers", "COALESCE(finished_at, started_at)"),
            ("incidents", "COALESCE(ended_at, started_at)"),
            ("discovery_runs", "COALESCE(finished_at, started_at)"),
        )
        deleted = 0
        with self._lock, self._conn:
            for table, column in targets:
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?",
                    (cutoff,),
                )
                deleted += int(cur.rowcount or 0)
        return deleted

    def checkpoint(self) -> None:
        """Truncate the WAL after a retention purge or a long soak."""

        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass


__all__ = ["SCHEMA_SQL", "QualificationStore"]
