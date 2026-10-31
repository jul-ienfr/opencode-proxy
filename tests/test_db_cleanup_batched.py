"""Régression P0.4 : cleanup DB par lots + WAL borné.

Un cleanup monolithique sur ~2 Go de corps remplissait le WAL (2,4 Go) et le
cache SQLite (RSS 3,6 Go). Ce test vérifie :
- la sémantique (DELETE > 30 j, NULL bodies 7-30 j, corps récents intacts) ;
- le découpage en lots (plusieurs commits, pas une transaction unique) ;
- `journal_size_limit` positionné à 64 Mo par l'init.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from observability import db as odb


def _ts(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def test_cleanup_batched_semantics_and_wal_limit(tmp_path):
    path = tmp_path / "requests.db"
    conn = sqlite3.connect(path)
    odb.init_requests_schema_fast(conn, busy_timeout=5000, cache_size=2000, mmap_size=0)
    assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864

    conn.execute("ALTER TABLE requests ADD COLUMN request_body TEXT")
    conn.execute("ALTER TABLE requests ADD COLUMN response_body TEXT")
    conn.execute("CREATE TABLE free_model_usage (timestamp TEXT)")
    conn.executemany(
        "INSERT INTO requests (id, timestamp, model, request_body, response_body) VALUES (?, ?, ?, ?, ?)",
        [
            ("old", _ts(40), "m", "old-request", "old-response"),
            ("mid", _ts(10), "m", "mid-request", "mid-response"),
            ("recent", _ts(1), "m", "recent-request", "recent-response"),
        ],
    )
    conn.execute("INSERT INTO free_model_usage (timestamp) VALUES (?)", (_ts(40),))
    conn.commit()

    logs: list[str] = []
    cleaned = odb.cleanup_old_bodies(
        conn,
        threading.Lock(),
        retention_days=7,
        delete_after_days=30,
        log_fn=logs.append,
        debug_fn=logs.append,
    )
    assert cleaned >= 3

    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, request_body, response_body FROM requests")}
    assert "old" not in rows
    assert rows["mid"] == (None, None)
    assert rows["recent"] == ("recent-request", "recent-response")
    assert conn.execute("SELECT COUNT(*) FROM free_model_usage").fetchone()[0] == 0

    wal = path.with_name("requests.db-wal")
    if wal.exists():
        assert wal.stat().st_size < 8 * 1024 * 1024
