"""T025 — plafond de corps configurable : writer, re-tronquage, rétention."""

from __future__ import annotations

import datetime
import sqlite3
import threading
from pathlib import Path

from observability import db as app_db

ROOT = Path(__file__).resolve().parent.parent
_NOW = datetime.datetime.now(datetime.UTC)
_YOUNG = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
_OLD = (_NOW - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_ANCIENT = (_NOW - datetime.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(n: int) -> dict:
    return {"model": "glm-5.1", "messages": [{"role": "user", "content": "x" * n}]}


def _init(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    app_db.init_requests_schema_fast(conn, busy_timeout=5000, cache_size=2000, mmap_size=0)
    app_db.init_free_usage_schema(conn)
    app_db.migrate_and_canary(conn)
    return conn


def _insert(conn, rid, ts, body) -> None:
    conn.execute(
        "INSERT INTO requests (id, timestamp, model, request_body) VALUES (?, ?, ?, ?)",
        (rid, ts, "m", body),
    )
    conn.commit()


def test_truncate_explicit_cap() -> None:
    out = app_db.truncate_body_for_storage(_body(50_000), 500)
    assert out is not None and len(out) <= 500


def test_truncate_none_reads_module_global(monkeypatch) -> None:
    monkeypatch.setattr(app_db, "MAX_BODY_STORAGE", 700)
    out = app_db.truncate_body_for_storage(_body(50_000))
    assert out is not None and len(out) <= 700


def test_materialize_passes_cap() -> None:
    raw = app_db.DbRowRaw((), (), request_body=_body(50_000), response_body=None, tools=[], tools_used=[])
    row = app_db.materialize_db_row(raw, redact_fn=lambda s: s, max_body_chars=400)
    assert row[2] is not None and len(row[2]) <= 400
    assert row[3] is None


def test_cleanup_retruncates_existing_bodies(tmp_path) -> None:
    conn = _init(str(tmp_path / "requests.db"))
    try:
        _insert(conn, "a", _YOUNG, "x" * 50_000)
        total = app_db.cleanup_old_bodies(
            conn,
            threading.Lock(),
            7,
            30,
            log_fn=lambda *_: None,
            debug_fn=lambda *_: None,
            max_body_chars=1000,
        )
        assert total >= 1
        assert conn.execute("SELECT LENGTH(request_body) FROM requests WHERE id='a'").fetchone()[0] <= 1000
    finally:
        conn.close()


def test_cleanup_without_cap_keeps_bodies(tmp_path) -> None:
    conn = _init(str(tmp_path / "requests.db"))
    try:
        _insert(conn, "a", _YOUNG, "x" * 50_000)
        app_db.cleanup_old_bodies(conn, threading.Lock(), 7, 30, log_fn=lambda *_: None, debug_fn=lambda *_: None)
        assert conn.execute("SELECT LENGTH(request_body) FROM requests WHERE id='a'").fetchone()[0] == 50_000
    finally:
        conn.close()


def test_cleanup_nulls_bodies_past_retention(tmp_path) -> None:
    conn = _init(str(tmp_path / "requests.db"))
    try:
        _insert(conn, "old", _OLD, "x" * 50_000)
        app_db.cleanup_old_bodies(
            conn,
            threading.Lock(),
            2,
            30,
            log_fn=lambda *_: None,
            debug_fn=lambda *_: None,
            max_body_chars=1000,
        )
        assert conn.execute("SELECT request_body FROM requests WHERE id='old'").fetchone()[0] is None
    finally:
        conn.close()


def test_cleanup_deletes_ancient_rows(tmp_path) -> None:
    conn = _init(str(tmp_path / "requests.db"))
    try:
        _insert(conn, "ancient", _ANCIENT, "x" * 100)
        app_db.cleanup_old_bodies(conn, threading.Lock(), 2, 30, log_fn=lambda *_: None, debug_fn=lambda *_: None)
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE id='ancient'").fetchone()[0] == 0
    finally:
        conn.close()


def test_opencode_wires_cap_to_writer_and_cleanup() -> None:
    src = (ROOT / "opencode.py").read_text(encoding="utf-8")
    assert 'yaml_get("database", "max_body_chars"' in src
    assert "max_body_chars=MAX_BODY_STORAGE" in src
    assert "_db_cleanup_old_bodies, retention, 30, cap" in src
