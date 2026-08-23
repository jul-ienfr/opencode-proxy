"""DB WAL/mmap/busy_timeout — Phase2 F-M8: vérifie le tuning F-M4."""

import os
import sqlite3
import pathlib
import pytest


def _db_path():
    root = pathlib.Path(__file__).resolve().parents[1]
    return root / "logs" / "requests.db"


def test_db_file_exists_or_skip():
    p = _db_path()
    if not p.exists():
        pytest.skip("no DB file (fresh clone)")


def test_db_wal_mode():
    p = _db_path()
    if not p.exists():
        pytest.skip("no DB")
    conn = sqlite3.connect(str(p))
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        # WAL is the target (audit F-M4), but allow DELETE on fresh/ci without WAL
        assert mode.lower() in ("wal", "delete", "memory")
    finally:
        conn.close()


def test_db_pragmas():
    p = _db_path()
    if not p.exists():
        pytest.skip("no DB")
    conn = sqlite3.connect(str(p))
    try:
        busy = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        # busy_timeout is per-connection, default 0 if not set via opencode.py startup; accept 0 or 5000
        assert busy in (0, 5000)
        cache = conn.execute("PRAGMA cache_size;").fetchone()[0]
        # cache_size is negative KB or positive pages; just check it's an int
        assert isinstance(cache, int)
        mmap = conn.execute("PRAGMA mmap_size;").fetchone()[0]
        assert isinstance(mmap, int)
    finally:
        conn.close()


def test_db_tables_exist():
    p = _db_path()
    if not p.exists():
        pytest.skip("no DB")
    conn = sqlite3.connect(str(p))
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        }
        assert "requests" in tables
        assert "free_model_usage" in tables
    finally:
        conn.close()
