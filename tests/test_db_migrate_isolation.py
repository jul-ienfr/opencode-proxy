"""T026 — isolation de la migration SQLite (connexion dédiée, pas de partage)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _init(path: str) -> None:
    from observability import db as app_db

    conn = sqlite3.connect(path)
    try:
        app_db.init_requests_schema_fast(conn, busy_timeout=5000, cache_size=2000, mmap_size=0)
        app_db.migrate_and_canary(conn)
    finally:
        conn.close()


def test_migrate_on_dedicated_connection_adds_columns(tmp_path) -> None:
    from observability import db as app_db

    path = str(tmp_path / "requests.db")
    conn = sqlite3.connect(path)
    try:
        app_db.init_requests_schema_fast(conn, busy_timeout=5000, cache_size=2000, mmap_size=0)
    finally:
        conn.close()

    before = app_db.db_maintenance_errors
    mig = sqlite3.connect(path, timeout=30)
    try:
        naive = app_db.migrate_and_canary(mig)
    finally:
        mig.close()
    assert isinstance(naive, int)
    assert app_db.db_maintenance_errors == before

    check = sqlite3.connect(path)
    try:
        cols = {row[1] for row in check.execute("PRAGMA table_info(requests)")}
    finally:
        check.close()
    assert {"account_alias", "free_model_ip", "station"} <= cols


def test_migrate_idempotent(tmp_path) -> None:
    from observability import db as app_db

    path = str(tmp_path / "requests.db")
    _init(path)
    before = app_db.db_maintenance_errors
    for _ in range(2):
        conn = sqlite3.connect(path, timeout=30)
        try:
            app_db.migrate_and_canary(conn)
        finally:
            conn.close()
    assert app_db.db_maintenance_errors == before


def test_concurrent_writer_and_migration_no_systemerror(tmp_path) -> None:
    from observability import db as app_db

    path = str(tmp_path / "requests.db")
    _init(path)
    before = app_db.db_maintenance_errors
    errors: list[BaseException] = []
    stop = threading.Event()

    def _writer() -> None:
        conn = sqlite3.connect(path, timeout=30)
        try:
            i = 0
            while not stop.is_set():
                conn.execute(
                    "INSERT INTO requests (id, timestamp, model, success) VALUES (?, ?, ?, 1)",
                    (f"row-{i}", "2026-11-16T00:00:00Z", "test-model"),
                )
                conn.commit()
                i += 1
                time.sleep(0.002)
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=_writer, daemon=True)
    thread.start()
    try:
        for _ in range(5):
            conn = sqlite3.connect(path, timeout=30)
            try:
                app_db.migrate_and_canary(conn)
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not errors, repr(errors)
    assert app_db.db_maintenance_errors == before


def test_db_migrate_bg_uses_dedicated_connection() -> None:
    src = (ROOT / "opencode.py").read_text(encoding="utf-8")
    start = src.index("async def _db_migrate_bg()")
    end = src.index("# ── Free model usage tracking", start)
    block = src[start:end]
    assert "sqlite3.connect(_db_path" in block
    assert "migrate_and_canary(_conn)" not in block
    assert "with _db_commit_lock:" not in block
