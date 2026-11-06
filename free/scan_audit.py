"""Persistent audit trail for fresh-IP scan runs.

One row per chain run and one row per candidate attempt. The audit is
fail-soft: a database problem must never stop a recovery scan. It gives the
operator a replayable history and lets a restarted supervisor close runs that
were interrupted by a crash.

No network, no Docker: pure SQLite around an explicit clock.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


class ScanAudit:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        root = Path(__file__).resolve().parent.parent
        self._path = Path(db_path) if db_path else root / "logs" / "registry.db"
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(str(self._path), timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._ensure_schema()
        except Exception:
            self._conn = None

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id TEXT PRIMARY KEY,
                    station INTEGER NOT NULL,
                    transport TEXT,
                    reason TEXT,
                    candidates INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    outcome TEXT,
                    winner_host TEXT,
                    winner_ip TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    station INTEGER NOT NULL,
                    hostname TEXT NOT NULL,
                    country TEXT,
                    transport TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT,
                    egress_ip TEXT,
                    valid INTEGER,
                    validation TEXT,
                    fingerprint TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_scan_attempts_run
                    ON scan_attempts(run_id);
                CREATE INDEX IF NOT EXISTS idx_scan_runs_open
                    ON scan_runs(ended_at, started_at);
                """
            )

    def _now(self) -> float:
        return float(self._clock())

    def start_run(
        self,
        station: int,
        *,
        transport: str = "",
        reason: str = "",
        candidates: int = 0,
    ) -> str:
        run_id = uuid.uuid4().hex
        if self._conn is None:
            return run_id
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO scan_runs("
                    "run_id, station, transport, reason, candidates, started_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        int(station),
                        str(transport or ""),
                        str(reason or ""),
                        int(candidates),
                        self._now(),
                    ),
                )
        except Exception:
            pass
        return run_id

    def begin_attempt(
        self,
        run_id: str,
        station: int,
        hostname: str,
        *,
        country: str = "",
        transport: str = "",
    ) -> str:
        attempt_id = uuid.uuid4().hex
        if self._conn is None:
            return attempt_id
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO scan_attempts("
                    "attempt_id, run_id, station, hostname, country, transport, "
                    "started_at, status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, 'intent')",
                    (
                        attempt_id,
                        str(run_id),
                        int(station),
                        str(hostname),
                        str(country or ""),
                        str(transport or ""),
                        self._now(),
                    ),
                )
        except Exception:
            pass
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        *,
        egress_ip: str = "",
        valid: bool | None = None,
        validation: str | None = None,
        fingerprint: str = "",
        error: str = "",
    ) -> None:
        if self._conn is None or not attempt_id:
            return
        try:
            with self._conn:
                self._conn.execute(
                    "UPDATE scan_attempts SET "
                    "finished_at=?, status=?, egress_ip=?, valid=?, "
                    "validation=?, fingerprint=?, error=? "
                    "WHERE attempt_id=?",
                    (
                        self._now(),
                        str(status or ""),
                        str(egress_ip or ""),
                        None if valid is None else int(bool(valid)),
                        str(validation or ""),
                        str(fingerprint or ""),
                        str(error or "")[:500],
                        str(attempt_id),
                    ),
                )
        except Exception:
            pass

    def finish_run(
        self,
        run_id: str,
        outcome: str,
        *,
        winner_host: str = "",
        winner_ip: str = "",
    ) -> None:
        if self._conn is None or not run_id:
            return
        try:
            with self._conn:
                self._conn.execute(
                    "UPDATE scan_runs SET ended_at=?, outcome=?, "
                    "winner_host=?, winner_ip=? WHERE run_id=?",
                    (
                        self._now(),
                        str(outcome or ""),
                        str(winner_host or ""),
                        str(winner_ip or ""),
                        str(run_id),
                    ),
                )
        except Exception:
            pass

    def reconcile_stale(self, *, max_age_s: float = 7200.0) -> int:
        """Close runs/attempts left open by a crash or forced stop."""

        if self._conn is None:
            return 0
        cutoff = self._now() - max(60.0, float(max_age_s))
        closed = 0
        try:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE scan_runs SET ended_at=?, outcome='interrupted' "
                    "WHERE ended_at IS NULL AND started_at < ?",
                    (self._now(), cutoff),
                )
                closed = int(cur.rowcount or 0)
                self._conn.execute(
                    "UPDATE scan_attempts SET finished_at=?, status='interrupted' "
                    "WHERE finished_at IS NULL AND run_id IN ("
                    "SELECT run_id FROM scan_runs WHERE outcome='interrupted')",
                    (self._now(),),
                )
        except Exception:
            return 0
        return closed

    def open_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Runs that have not reached a terminal outcome yet."""

        if self._conn is None:
            return []
        try:
            self._conn.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM scan_runs WHERE ended_at IS NULL "
                    "ORDER BY started_at ASC LIMIT ?",
                    (max(1, int(limit)),),
                )
            ]
        except Exception:
            return []

    def interrupt_attempts(self, run_id: str) -> None:
        """Close attempts that never reached a terminal status."""

        if self._conn is None or not run_id:
            return
        try:
            with self._conn:
                self._conn.execute(
                    "UPDATE scan_attempts SET finished_at=?, status='interrupted' "
                    "WHERE run_id=? AND finished_at IS NULL",
                    (self._now(), str(run_id)),
                )
        except Exception:
            pass

    def snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        if self._conn is None:
            return {"runs": [], "attempts": []}
        try:
            self._conn.row_factory = sqlite3.Row
            runs = [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                )
            ]
            attempts = [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM scan_attempts ORDER BY started_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                )
            ]
            return {"runs": runs, "attempts": attempts}
        except Exception:
            return {"runs": [], "attempts": []}

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


__all__ = ["ScanAudit"]
