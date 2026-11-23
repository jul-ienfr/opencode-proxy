"""Budget de sondes séparé du ledger client — 023 (T024).

Une IP ne peut être sondée que ``probe_budget_per_ip`` fois par fenêtre et
jamais avant ``probe_cooldown_s``. Une IP ``proven``/``serving`` n'est jamais
sondée par la qualification (principe XVIII).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PER_IP = 2
DEFAULT_COOLDOWN_S = 600.0
WINDOW_S = 24 * 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_budget (
    provider TEXT NOT NULL,
    bucket_key TEXT NOT NULL,
    window_start REAL NOT NULL DEFAULT 0,
    probe_count INTEGER NOT NULL DEFAULT 0,
    client_count INTEGER NOT NULL DEFAULT 0,
    last_probe_at REAL,
    PRIMARY KEY (provider, bucket_key)
)
"""


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    probe_count: int = 0
    reason: str = ""


class ProbeBudget:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        per_ip: int = DEFAULT_PER_IP,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        if path is None:
            from free.ip_registry.registry_db import registry_db_path

            path = registry_db_path()
        self.path = Path(path)
        self.per_ip = max(0, int(per_ip))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _row(self, conn: sqlite3.Connection, provider: str, bucket: str):
        return conn.execute(
            "SELECT window_start, probe_count, client_count, last_probe_at "
            "FROM probe_budget WHERE provider=? AND bucket_key=?",
            (provider, bucket),
        ).fetchone()

    def allow(
        self,
        provider: str,
        bucket_key: str,
        *,
        now: float | None = None,
    ) -> BudgetDecision:
        ts = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            row = self._row(conn, provider, bucket_key)
        if row is None:
            return BudgetDecision(True, 0, "first probe")
        window_start, count, _clients, last = row
        if ts - float(window_start or 0.0) >= WINDOW_S:
            return BudgetDecision(True, 0, "new window")
        if last is not None and ts - float(last) < self.cooldown_s:
            return BudgetDecision(False, int(count), "probe cooldown")
        if int(count) >= self.per_ip:
            return BudgetDecision(False, int(count), "probe budget exhausted")
        return BudgetDecision(True, int(count), "budget available")

    def record_probe(
        self,
        provider: str,
        bucket_key: str,
        *,
        now: float | None = None,
    ) -> int:
        ts = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            row = self._row(conn, provider, bucket_key)
            if row is None or ts - float(row[0] or 0.0) >= WINDOW_S:
                conn.execute(
                    "INSERT INTO probe_budget (provider, bucket_key, window_start, "
                    "probe_count, client_count, last_probe_at) VALUES (?,?,?,1,0,?) "
                    "ON CONFLICT(provider, bucket_key) DO UPDATE SET "
                    "window_start=excluded.window_start, probe_count=1, last_probe_at=excluded.last_probe_at",
                    (provider, bucket_key, ts, ts),
                )
                return 1
            conn.execute(
                "UPDATE probe_budget SET probe_count=probe_count+1, last_probe_at=? "
                "WHERE provider=? AND bucket_key=?",
                (ts, provider, bucket_key),
            )
            row = self._row(conn, provider, bucket_key)
        return int(row[1]) if row else 1

    def record_client(
        self,
        provider: str,
        bucket_key: str,
        *,
        now: float | None = None,
    ) -> int:
        ts = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            row = self._row(conn, provider, bucket_key)
            if row is None or ts - float(row[0] or 0.0) >= WINDOW_S:
                conn.execute(
                    "INSERT INTO probe_budget (provider, bucket_key, window_start, "
                    "probe_count, client_count, last_probe_at) VALUES (?,?,?,0,1,NULL) "
                    "ON CONFLICT(provider, bucket_key) DO UPDATE SET "
                    "window_start=excluded.window_start, client_count=1",
                    (provider, bucket_key, ts),
                )
                return 1
            conn.execute(
                "UPDATE probe_budget SET client_count=client_count+1 "
                "WHERE provider=? AND bucket_key=?",
                (provider, bucket_key),
            )
            row = self._row(conn, provider, bucket_key)
        return int(row[2]) if row else 1

    def reset(self, provider: str, bucket_key: str) -> None:
        """Efface le budget d'un bucket (réparation d'une classification)."""
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM probe_budget WHERE provider=? AND bucket_key=?",
                (provider, bucket_key),
            )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, bucket_key, window_start, probe_count, "
                "client_count, last_probe_at FROM probe_budget ORDER BY provider, bucket_key"
            ).fetchall()
        keys = (
            "provider", "bucket_key", "window_start", "probe_count",
            "client_count", "last_probe_at",
        )
        return [dict(zip(keys, r)) for r in rows]
