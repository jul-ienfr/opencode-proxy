"""Per-station repair requests shared by the pool and the supervisor.

The pool knows a real request just received a 429; the supervisor owns the
repair. This small SQLite queue is the explicit hand-off between them, so a
per-model 429 cannot be hidden by a global ``model_usable`` flag.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "logs" / "repair_queue.db"


class RepairQueue:
    def __init__(self, path: str | Path | None = None, *, ttl_s: float = 1800.0) -> None:
        self._path = str(path or DEFAULT_PATH)
        self._ttl_s = max(30.0, float(ttl_s))
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_requests (
                station      INTEGER PRIMARY KEY,
                reason       TEXT NOT NULL,
                model        TEXT,
                ip           TEXT,
                hostname     TEXT,
                requested_at REAL NOT NULL,
                expires_at   REAL NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                claimed_at   REAL
            );
            CREATE INDEX IF NOT EXISTS idx_repair_pending
                ON repair_requests(status, expires_at);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def request(
        self,
        station: int,
        reason: str,
        *,
        model: str | None = None,
        ip: str | None = None,
        hostname: str | None = None,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO repair_requests(
                    station, reason, model, ip, hostname, requested_at,
                    expires_at, status, claimed_at
                ) VALUES(?,?,?,?,?,?,?,'pending',NULL)
                ON CONFLICT(station) DO UPDATE SET
                    reason=excluded.reason,
                    model=excluded.model,
                    ip=excluded.ip,
                    hostname=excluded.hostname,
                    requested_at=excluded.requested_at,
                    expires_at=excluded.expires_at,
                    status='pending',
                    claimed_at=NULL
                """,
                (
                    int(station),
                    str(reason or "repair"),
                    str(model or ""),
                    str(ip or ""),
                    str(hostname or ""),
                    ts,
                    ts + self._ttl_s,
                ),
            )

    def pending(self, now: float | None = None) -> list[dict[str, Any]]:
        ts = time.time() if now is None else float(now)
        with self._conn:
            rows = self._conn.execute(
                "SELECT * FROM repair_requests "
                "WHERE expires_at > ? AND status IN ('pending','claimed') "
                "ORDER BY requested_at ASC",
                (ts,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim(self, station: int, now: float | None = None) -> None:
        ts = time.time() if now is None else float(now)
        with self._conn:
            self._conn.execute(
                "UPDATE repair_requests SET status='claimed', claimed_at=? "
                "WHERE station=? AND expires_at > ?",
                (ts, int(station), ts),
            )

    def complete(self, station: int) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM repair_requests WHERE station=?", (int(station),)
            )

    def prune(self, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM repair_requests WHERE expires_at <= ?", (ts,)
            )
        return int(cur.rowcount or 0)
