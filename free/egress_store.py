"""SQLite store for the provider-agnostic egress catalog.

The store lives in ``registry.db`` next to the existing registry tables.  It
owns only ``egress_*`` tables and never rewrites the historical
``egress_ips``/``observations`` rows.  Every write is transactional; the
catalog is reconstructible from options + observations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from free.egress_catalog import (
    EgressOption,
    Observation,
    OptionStatus,
    ProviderPolicy,
    score_option,
    transition,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS egress_sources (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    url          TEXT NOT NULL DEFAULT '',
    scheme       TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    interval_s   INTEGER NOT NULL DEFAULT 21600,
    config_json  TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_run_at  REAL,
    last_error   TEXT,
    candidates_count INTEGER NOT NULL DEFAULT 0,
    usable_count     INTEGER NOT NULL DEFAULT 0,
    last_promoted_at REAL
);

CREATE TABLE IF NOT EXISTS egress_options (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL UNIQUE,
    provider        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    host            TEXT NOT NULL DEFAULT '',
    port            INTEGER NOT NULL DEFAULT 0,
    station         TEXT NOT NULL DEFAULT '',
    egress_ip       TEXT NOT NULL DEFAULT '',
    block           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    last_checked_at REAL NOT NULL DEFAULT 0,
    expires_at      REAL NOT NULL DEFAULT 0,
    reset_at        REAL NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_status     INTEGER NOT NULL DEFAULT 0,
    last_latency_ms REAL NOT NULL DEFAULT 0,
    score           REAL NOT NULL DEFAULT 0,
    source_ids      TEXT NOT NULL DEFAULT '[]',
    meta_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_egress_options_provider_status
    ON egress_options(provider, status);
CREATE INDEX IF NOT EXISTS idx_egress_options_expires
    ON egress_options(provider, expires_at);
CREATE INDEX IF NOT EXISTS idx_egress_options_block
    ON egress_options(provider, block);
CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_options_egress
    ON egress_options(provider, egress_ip)
    WHERE egress_ip <> '';

CREATE TABLE IF NOT EXISTS egress_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    option_id   INTEGER NOT NULL REFERENCES egress_options(id) ON DELETE CASCADE,
    observed_at REAL NOT NULL,
    phase       TEXT NOT NULL,
    status      INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL,
    latency_ms  REAL NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_egress_obs_option_time
    ON egress_observations(option_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_egress_obs_time
    ON egress_observations(observed_at);

CREATE TABLE IF NOT EXISTS egress_catalog_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS egress_rotation_slots (
    provider    TEXT PRIMARY KEY,
    option_id   INTEGER REFERENCES egress_options(id),
    selected_at REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    expires_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog_runs (
    id          TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL DEFAULT 'running',
    candidates  INTEGER NOT NULL DEFAULT 0,
    promoted    INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    dead        INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_catalog_runs_provider_time
    ON catalog_runs(provider, started_at DESC);

CREATE TABLE IF NOT EXISTS egress_provider_state (
    provider      TEXT PRIMARY KEY,
    known         INTEGER NOT NULL DEFAULT 0,
    reachable     INTEGER NOT NULL DEFAULT 0,
    usable        INTEGER NOT NULL DEFAULT 0,
    quota         INTEGER NOT NULL DEFAULT 0,
    auth          INTEGER NOT NULL DEFAULT 0,
    dead          INTEGER NOT NULL DEFAULT 0,
    unique_blocks INTEGER NOT NULL DEFAULT 0,
    last_run_at   REAL,
    last_ok_at    REAL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS egress_threat_intel (
    indicator TEXT NOT NULL,
    kind      TEXT NOT NULL,          -- ip|cidr
    source    TEXT NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (indicator, source)
);
CREATE INDEX IF NOT EXISTS idx_threat_intel_source
    ON egress_threat_intel(source);

CREATE TABLE IF NOT EXISTS egress_threat_feeds (
    source     TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT ''
);
"""


class EgressStore:
    """Thread-safe, WAL SQLite catalog."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        retention_days: float = 7.0,
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._retention_s = max(1.0, float(retention_days)) * 86_400.0
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(egress_options)")
            }
            if "score" not in columns:
                self._conn.execute(
                    "ALTER TABLE egress_options ADD COLUMN score REAL NOT NULL DEFAULT 0"
                )
            source_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(egress_sources)")
            }
            for name, sql_type in (
                ("candidates_count", "INTEGER NOT NULL DEFAULT 0"),
                ("usable_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_promoted_at", "REAL"),
            ):
                if name not in source_columns:
                    self._conn.execute(
                        f"ALTER TABLE egress_sources ADD COLUMN {name} {sql_type}"
                    )
            # Public/NordVPN rotate on one egress IP.  Older rows stored a
            # /24 block; normalize them in place (no destructive rebuild).
            self._conn.execute(
                "UPDATE egress_options SET block=egress_ip "
                "WHERE provider IN ('public','nordvpn') "
                "AND egress_ip <> '' AND block LIKE '%/%'"
            )
            self._conn.commit()

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def path(self) -> str:
        return self._path

    def _now(self) -> float:
        return float(self._clock())

    # ── sources ───────────────────────────────────────────────────────────

    def upsert_source(self, source: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        payload = {
            "id": str(source.get("id") or "").strip(),
            "type": str(source.get("type") or "").strip(),
            "url": str(source.get("url") or "").strip(),
            "scheme": str(source.get("scheme") or "").strip(),
            "enabled": 1 if bool(source.get("enabled", True)) else 0,
            "interval_s": max(60, int(source.get("interval_s") or 21_600)),
            "config_json": json.dumps(source.get("config") or {}, ensure_ascii=False, sort_keys=True),
            "created_at": now,
            "updated_at": now,
        }
        if not payload["id"] or not payload["type"]:
            raise ValueError("source id/type requis")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO egress_sources(
                    id, type, url, scheme, enabled, interval_s, config_json,
                    created_at, updated_at
                ) VALUES(:id, :type, :url, :scheme, :enabled, :interval_s,
                         :config_json, :created_at, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    type=excluded.type, url=excluded.url, scheme=excluded.scheme,
                    enabled=excluded.enabled, interval_s=excluded.interval_s,
                    config_json=excluded.config_json, updated_at=excluded.updated_at
                """,
                payload,
            )
        return self.get_source(payload["id"]) or {}

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM egress_sources WHERE id=?", (str(source_id),)
            ).fetchone()
        return _source_row(row) if row else None

    def list_sources(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM egress_sources"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [_source_row(row) for row in rows]

    def delete_source(self, source_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM egress_sources WHERE id=?", (str(source_id),)
            )
        return int(cursor.rowcount or 0) > 0

    def mark_source_run(self, source_id: str, *, error: str = "") -> None:
        now = self._now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE egress_sources SET last_run_at=?, last_error=? WHERE id=?",
                (now, str(error or "")[:300], str(source_id)),
            )

    def record_source_candidates(self, source_id: str, count: int) -> None:
        if int(count or 0) <= 0:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE egress_sources SET candidates_count=?, updated_at=? WHERE id=?",
                (int(count), self._now(), str(source_id)),
            )

    def record_source_promotion(self, source_id: str, count: int = 1) -> None:
        if int(count or 0) <= 0:
            return
        now = self._now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE egress_sources SET usable_count=usable_count+?, "
                "last_promoted_at=?, updated_at=? WHERE id=?",
                (int(count), now, now, str(source_id)),
            )

    def sync_source_usable_counts(self) -> int:
        """Align source counters with the currently usable options."""

        counts: dict[str, int] = {}
        for option in self.list_options(status=OptionStatus.USABLE, limit=5_000):
            for source_id in option.source_ids:
                counts[source_id] = counts.get(source_id, 0) + 1
        if not counts:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE egress_sources SET usable_count=0, updated_at=?",
                    (self._now(),),
                )
            return 0
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE egress_sources SET usable_count=0, updated_at=?",
                (self._now(),),
            )
            for source_id, count in counts.items():
                self._conn.execute(
                    "UPDATE egress_sources SET usable_count=?, updated_at=? WHERE id=?",
                    (int(count), self._now(), str(source_id)),
                )
        return len(counts)

    # ── meta ──────────────────────────────────────────────────────────────

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM egress_catalog_meta WHERE key=?", (str(key),)
            ).fetchone()
        return str(row["value"]) if row else str(default)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO egress_catalog_meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )

    # ── threat intel ──────────────────────────────────────────────────────

    def replace_threat_indicators(
        self,
        source: str,
        url: str,
        *,
        ips: Iterable[str],
        networks: Iterable[str],
        now: float | None = None,
    ) -> int:
        ts = self._now() if now is None else float(now)
        rows = [(str(ip), "ip", str(source), ts) for ip in ips]
        rows += [(str(net), "cidr", str(source), ts) for net in networks]
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM egress_threat_intel WHERE source=?", (str(source),))
            self._conn.executemany(
                "INSERT OR REPLACE INTO egress_threat_intel(indicator, kind, source, last_seen) "
                "VALUES(?,?,?,?)",
                rows,
            )
            self._conn.execute(
                "INSERT INTO egress_threat_feeds(source, url, fetched_at, count, error) "
                "VALUES(?,?,?,?, '') "
                "ON CONFLICT(source) DO UPDATE SET url=excluded.url, "
                "fetched_at=excluded.fetched_at, count=excluded.count, error=''",
                (str(source), str(url), ts, len(rows)),
            )
        return len(rows)

    def record_threat_feed_error(
        self,
        source: str,
        url: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        ts = self._now() if now is None else float(now)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO egress_threat_feeds(source, url, fetched_at, count, error) "
                "VALUES(?,?,?,0,?) "
                "ON CONFLICT(source) DO UPDATE SET url=excluded.url, "
                "error=excluded.error",
                (str(source), str(url), ts, str(error)[:300]),
            )

    def threat_indicators(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT indicator, kind, source, last_seen FROM egress_threat_intel"
            ).fetchall()
        return [dict(row) for row in rows]

    def threat_feeds(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM egress_threat_feeds ORDER BY source"
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_threat_feeds(self, valid_sources: Iterable[str]) -> int:
        keep = {str(source) for source in valid_sources}
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT source FROM egress_threat_feeds").fetchall()
            stale = [str(row["source"]) for row in rows if str(row["source"]) not in keep]
            for source in stale:
                self._conn.execute("DELETE FROM egress_threat_intel WHERE source=?", (source,))
                self._conn.execute("DELETE FROM egress_threat_feeds WHERE source=?", (source,))
        return len(stale)

    # ── options ───────────────────────────────────────────────────────────

    def upsert_option(self, option: EgressOption) -> int:
        """Insert or merge an option, deduplicating by provider + egress IP."""

        with self._lock, self._conn:
            return self._upsert_option_locked(option)

    def _upsert_option_locked(self, option: EgressOption) -> int:
        now = self._now()
        key = self._resolve_key(option)
        existing = self._conn.execute(
            "SELECT * FROM egress_options WHERE key=?", (key,)
        ).fetchone()
        if existing is None:
            cursor = self._conn.execute(
                """
                INSERT INTO egress_options(
                    key, provider, kind, host, port, station, egress_ip,
                    block, status, first_seen_at, last_seen_at,
                    last_checked_at, expires_at, reset_at, success_count,
                    fail_count, last_status, last_latency_ms, score, source_ids,
                    meta_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    option.provider,
                    option.kind,
                    option.host,
                    int(option.port or 0),
                    option.station,
                    option.egress_ip,
                    option.block,
                    option.status,
                    float(option.first_seen_at or now),
                    float(option.last_seen_at or now),
                    float(option.last_checked_at or 0.0),
                    float(option.expires_at or 0.0),
                    float(option.reset_at or 0.0),
                    int(option.success_count or 0),
                    int(option.fail_count or 0),
                    int(option.last_status or 0),
                    float(option.last_latency_ms or 0.0),
                    float(option.score or 0.0),
                    _json(list(option.source_ids)),
                    _json(option.meta),
                ),
            )
            return int(cursor.lastrowid or 0)
        merged_sources = list(
            dict.fromkeys(
                [
                    *(_json_list(existing["source_ids"])),
                    *(option.source_ids or ()),
                ]
            )
        )
        merged_meta = {**_json_dict(existing["meta_json"]), **dict(option.meta or {})}
        # A candidate refresh must not downgrade a proven usable option.
        status = str(option.status)
        if str(option.status) == OptionStatus.CANDIDATE and str(existing["status"]) != OptionStatus.EXPIRED:
            status = str(existing["status"])
        keep_proof = (
            str(option.status) == OptionStatus.CANDIDATE
            and str(existing["status"]) == OptionStatus.USABLE
        )
        self._conn.execute(
            """
            UPDATE egress_options SET
                host=?, port=?, station=?, egress_ip=?, block=?,
                status=?, last_seen_at=?, last_checked_at=?,
                expires_at=?, reset_at=?, success_count=?, fail_count=?,
                last_status=?, last_latency_ms=?, score=?, source_ids=?, meta_json=?
            WHERE id=?
            """,
            (
                option.host or str(existing["host"]),
                int(option.port or existing["port"] or 0),
                option.station or str(existing["station"]),
                option.egress_ip or str(existing["egress_ip"]),
                option.block or str(existing["block"]),
                status,
                max(float(existing["last_seen_at"] or 0.0), float(option.last_seen_at or now)),
                max(float(existing["last_checked_at"] or 0.0), float(option.last_checked_at or 0.0)),
                float(existing["expires_at"] or 0.0)
                if keep_proof
                else float(option.expires_at or existing["expires_at"] or 0.0),
                float(existing["reset_at"] or 0.0)
                if keep_proof
                else float(option.reset_at or existing["reset_at"] or 0.0),
                max(int(existing["success_count"] or 0), int(option.success_count or 0)),
                max(int(existing["fail_count"] or 0), int(option.fail_count or 0)),
                int(option.last_status or existing["last_status"] or 0),
                float(option.last_latency_ms or existing["last_latency_ms"] or 0.0),
                float(option.score or existing["score"] or 0.0),
                _json(merged_sources),
                _json(merged_meta),
                int(existing["id"]),
            ),
        )
        return int(existing["id"])

    def _resolve_key(self, option: EgressOption) -> str:
        if option.egress_ip:
            row = self._conn.execute(
                "SELECT key FROM egress_options WHERE provider=? AND egress_ip=? LIMIT 1",
                (option.provider, option.egress_ip),
            ).fetchone()
            if row is not None:
                return str(row["key"])
        return option.key

    def apply(
        self,
        option: EgressOption,
        observation: Observation,
        policy: ProviderPolicy,
        *,
        reset_at: float = 0.0,
        run_id: str = "",
    ) -> EgressOption:
        """Persist an observation and the resulting immutable option."""

        existing = self.get_option(option.key)
        if existing is None and option.egress_ip:
            existing = self.get_by_egress(option.provider, option.egress_ip)
        base = existing or option
        next_option = transition(
            base,
            observation,
            policy,
            reset_at=reset_at,
            source_ids=base.source_ids,
        )
        next_option = next_option.with_updates(
            score=score_option(next_option, float(observation.observed_at or 0.0), policy)
        )
        with self._lock, self._conn:
            option_id = self._upsert_option_locked(next_option)
            self._conn.execute(
                """
                INSERT INTO egress_observations(
                    option_id, observed_at, phase, status, result, latency_ms,
                    message, run_id
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    option_id,
                    float(observation.observed_at or self._now()),
                    str(observation.phase),
                    int(observation.status or 0),
                    str(observation.result),
                    float(observation.latency_ms or 0.0),
                    str(observation.message or "")[:300],
                    str(run_id or observation.run_id or ""),
                ),
            )
        return next_option

    def get_option(self, key: str) -> EgressOption | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM egress_options WHERE key=?", (str(key),)
            ).fetchone()
        return _option_row(row) if row else None

    def get_by_egress(self, provider: str, egress_ip: str) -> EgressOption | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM egress_options WHERE provider=? AND egress_ip=?",
                (str(provider), str(egress_ip)),
            ).fetchone()
        return _option_row(row) if row else None

    def list_options(
        self,
        *,
        provider: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EgressOption]:
        clauses: list[str] = []
        values: list[Any] = []
        if provider:
            clauses.append("provider=?")
            values.append(str(provider))
        if status:
            clauses.append("status=?")
            values.append(str(status))
        if kind:
            clauses.append("kind=?")
            values.append(str(kind))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM egress_options "
            f"{where} ORDER BY status, last_checked_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (*values, max(1, min(5_000, int(limit))), max(0, int(offset)))
            ).fetchall()
        return [_option_row(row) for row in rows]

    def blocked_endpoints(self, provider: str, *, now: float | None = None) -> set[tuple[str, int]]:
        """Endpoints whose provider reset (429) or AUTH is still active."""

        ts = self._now() if now is None else float(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT host, port FROM egress_options "
                "WHERE provider=? AND (reset_at > ? OR status='auth' "
                "OR (status='quota' AND expires_at > ?))",
                (str(provider), ts, ts),
            ).fetchall()
        return {
            (str(row["host"]), int(row["port"]))
            for row in rows
            if str(row["host"]) and int(row["port"] or 0) > 0
        }

    def recompute_scores(self, policy: ProviderPolicy, *, now: float | None = None) -> int:
        """Backfill ``score`` from current counters (boot/migration)."""

        ts = self._now() if now is None else float(now)
        options = self.list_options(provider=policy.provider, limit=5_000)
        updates = [
            (float(score_option(option, ts, policy)), option.key)
            for option in options
        ]
        if not updates:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE egress_options SET score=? WHERE key=?",
                updates,
            )
        return len(updates)

    def selectable(
        self,
        policy: ProviderPolicy,
        *,
        now: float | None = None,
        limit: int | None = None,
    ) -> list[EgressOption]:
        ts = self._now() if now is None else float(now)
        sql = (
            "SELECT * FROM egress_options WHERE provider=? AND status=? "
            "AND expires_at > ? AND reset_at <= ? ORDER BY score DESC, last_checked_at DESC"
        )
        values: list[Any] = (policy.provider, OptionStatus.USABLE, ts, ts)
        if limit:
            sql += " LIMIT ?"
            values = (*values, max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [_option_row(row) for row in rows]

    def count_provider(self, provider: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM egress_options WHERE provider=?",
                (str(provider),),
            ).fetchone()
        return int(row[0] if row else 0)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, status, COUNT(*) AS n FROM egress_options GROUP BY provider, status"
            ).fetchall()
            blocks = self._conn.execute(
                "SELECT provider, COUNT(DISTINCT block) FROM egress_options "
                "WHERE block <> '' GROUP BY provider"
            ).fetchall()
            runs = self._conn.execute(
                "SELECT provider, MAX(finished_at) FROM catalog_runs GROUP BY provider"
            ).fetchall()
        providers: dict[str, dict[str, Any]] = {}
        for row in rows:
            provider = str(row["provider"])
            bucket = providers.setdefault(
                provider,
                {
                    "known": 0,
                    "reachable": 0,
                    "usable": 0,
                    "quota": 0,
                    "auth": 0,
                    "dead": 0,
                    "candidate": 0,
                    "unique_blocks": 0,
                    "last_run_at": 0.0,
                },
            )
            bucket[str(row["status"])] = int(row["n"])
            bucket["known"] += int(row["n"])
        for row in blocks:
            providers.setdefault(str(row[0]), {})["unique_blocks"] = int(row[1])
        for row in runs:
            providers.setdefault(str(row[0]), {})["last_run_at"] = float(row[1] or 0.0)
        for bucket in providers.values():
            for key in (
                "known",
                "reachable",
                "usable",
                "quota",
                "auth",
                "dead",
                "candidate",
                "unique_blocks",
            ):
                bucket.setdefault(key, 0)
            bucket.setdefault("last_run_at", 0.0)
        return providers

    # ── runs ──────────────────────────────────────────────────────────────

    def start_run(self, provider: str) -> str:
        run_id = f"run_{provider}_{uuid.uuid4().hex[:12]}"
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO catalog_runs(id, provider, started_at, status) VALUES(?,?,?,?)",
                (run_id, str(provider), self._now(), "running"),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "ok",
        candidates: int = 0,
        promoted: int = 0,
        quarantined: int = 0,
        dead: int = 0,
        errors: Iterable[str] = (),
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE catalog_runs SET finished_at=?, status=?, candidates=?,
                    promoted=?, quarantined=?, dead=?, errors_json=?
                WHERE id=?
                """,
                (
                    self._now(),
                    str(status),
                    int(candidates),
                    int(promoted),
                    int(quarantined),
                    int(dead),
                    _json([str(error)[:300] for error in errors]),
                    str(run_id),
                ),
            )
        self._refresh_provider_state()

    def _refresh_provider_state(self) -> None:
        summary = self.summary()
        now = self._now()
        with self._lock, self._conn:
            for provider, bucket in summary.items():
                self._conn.execute(
                    """
                    INSERT INTO egress_provider_state(
                        provider, known, reachable, usable, quota, auth, dead,
                        unique_blocks, last_run_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider) DO UPDATE SET
                        known=excluded.known, reachable=excluded.reachable,
                        usable=excluded.usable, quota=excluded.quota,
                        auth=excluded.auth, dead=excluded.dead,
                        unique_blocks=excluded.unique_blocks,
                        last_run_at=excluded.last_run_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(provider),
                        int(bucket.get("known", 0)),
                        int(bucket.get("reachable", 0)),
                        int(bucket.get("usable", 0)),
                        int(bucket.get("quota", 0)),
                        int(bucket.get("auth", 0)),
                        int(bucket.get("dead", 0)),
                        int(bucket.get("unique_blocks", 0)),
                        float(bucket.get("last_run_at", 0.0) or 0.0),
                        now,
                    ),
                )

    def provider_state(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM egress_provider_state ORDER BY provider"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_observations(
        self,
        *,
        key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT o.key, ob.observed_at, ob.phase, ob.status, ob.result, "
            "ob.latency_ms, ob.message, ob.run_id "
            "FROM egress_observations ob JOIN egress_options o ON o.id=ob.option_id"
        )
        values: list[Any] = []
        if key:
            sql += " WHERE o.key=?"
            values.append(str(key))
        sql += " ORDER BY ob.observed_at DESC LIMIT ?"
        values.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, *, provider: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM catalog_runs"
        values: list[Any] = []
        if provider:
            sql += " WHERE provider=?"
            values.append(str(provider))
        sql += " ORDER BY started_at DESC LIMIT ?"
        values.append(max(1, min(200, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [_run_row(row) for row in rows]

    # ── slots ─────────────────────────────────────────────────────────────

    def set_slot(self, provider: str, option_key: str, *, reason: str, expires_at: float) -> None:
        option = self.get_option(option_key)
        if option is None:
            raise KeyError(option_key)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO egress_rotation_slots(provider, option_id, selected_at, reason, expires_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(provider) DO UPDATE SET
                    option_id=excluded.option_id, selected_at=excluded.selected_at,
                    reason=excluded.reason, expires_at=excluded.expires_at
                """,
                (str(provider), self._option_id(option_key), self._now(), str(reason), float(expires_at)),
            )

    def _option_id(self, key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM egress_options WHERE key=?", (str(key),)
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return int(row[0])

    def slots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, o.key, o.provider, o.egress_ip, o.block, o.status "
                "FROM egress_rotation_slots s LEFT JOIN egress_options o ON o.id=s.option_id"
            ).fetchall()
        return {str(row["provider"]): dict(row) for row in rows}

    # ── retention / export ────────────────────────────────────────────────

    def purge(self, *, now: float | None = None) -> dict[str, int]:
        ts = self._now() if now is None else float(now)
        old_observations = ts - self._retention_s
        dead_cutoff = ts - 30 * 86_400.0
        with self._lock, self._conn:
            obs = self._conn.execute(
                "DELETE FROM egress_observations WHERE observed_at < ?",
                (old_observations,),
            ).rowcount
            dead = self._conn.execute(
                "DELETE FROM egress_options WHERE status=? AND last_seen_at < ?",
                (OptionStatus.DEAD, dead_cutoff),
            ).rowcount
            runs = self._conn.execute(
                "DELETE FROM catalog_runs WHERE started_at < ?",
                (ts - 30 * 86_400.0,),
            ).rowcount
        return {"observations": int(obs or 0), "options": int(dead or 0), "runs": int(runs or 0)}

    def export_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": self._now(),
            "providers": self.summary(),
            "runs": self.list_runs(limit=20),
            "options": [
                _option_payload(option)
                for option in self.list_options(limit=5_000)
            ],
        }
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _option_row(row: sqlite3.Row) -> EgressOption:
    return EgressOption(
        key=str(row["key"]),
        provider=str(row["provider"]),
        kind=str(row["kind"]),
        host=str(row["host"]),
        port=int(row["port"]),
        station=str(row["station"]),
        egress_ip=str(row["egress_ip"]),
        block=str(row["block"]),
        status=str(row["status"]),
        first_seen_at=float(row["first_seen_at"]),
        last_seen_at=float(row["last_seen_at"]),
        last_checked_at=float(row["last_checked_at"]),
        expires_at=float(row["expires_at"]),
        reset_at=float(row["reset_at"]),
        success_count=int(row["success_count"]),
        fail_count=int(row["fail_count"]),
        last_status=int(row["last_status"]),
        last_latency_ms=float(row["last_latency_ms"]),
        score=float(row["score"] or 0.0),
        source_ids=tuple(_json_list(row["source_ids"])),
        meta=_json_dict(row["meta_json"]),
    )


def _source_row(row: sqlite3.Row) -> dict[str, Any]:
    candidates = int(row["candidates_count"])
    usable = int(row["usable_count"])
    return {
        "id": str(row["id"]),
        "type": str(row["type"]),
        "url": str(row["url"]),
        "scheme": str(row["scheme"]),
        "enabled": bool(row["enabled"]),
        "interval_s": int(row["interval_s"]),
        "config": _json_dict(row["config_json"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "last_run_at": float(row["last_run_at"] or 0.0),
        "last_error": str(row["last_error"] or ""),
        "candidates_count": candidates,
        "usable_count": usable,
        "yield_pct": round(100.0 * usable / candidates, 2) if candidates else 0.0,
        "last_promoted_at": float(row["last_promoted_at"] or 0.0),
    }


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "provider": str(row["provider"]),
        "started_at": float(row["started_at"]),
        "finished_at": float(row["finished_at"] or 0.0),
        "status": str(row["status"]),
        "candidates": int(row["candidates"]),
        "promoted": int(row["promoted"]),
        "quarantined": int(row["quarantined"]),
        "dead": int(row["dead"]),
        "errors": _json_list(row["errors_json"]),
    }


def _option_payload(option: EgressOption) -> dict[str, Any]:
    return {
        "key": option.key,
        "provider": option.provider,
        "kind": option.kind,
        "host": option.host,
        "port": option.port,
        "station": option.station,
        "egress_ip": option.egress_ip,
        "block": option.block,
        "status": option.status,
        "first_seen_at": option.first_seen_at,
        "last_seen_at": option.last_seen_at,
        "last_checked_at": option.last_checked_at,
        "expires_at": option.expires_at,
        "reset_at": option.reset_at,
        "success_count": option.success_count,
        "fail_count": option.fail_count,
        "last_status": option.last_status,
        "last_latency_ms": option.last_latency_ms,
        "score": option.score,
        "source_ids": list(option.source_ids),
    }


__all__ = ["EgressStore"]
