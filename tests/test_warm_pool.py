"""Consommation du warm pool — feature 019, tâche T310."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fresh_chain_supervisor import _warm_pool_pick  # noqa: E402


def _db(tmp_path, rows):
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE warm_pool (ip TEXT PRIMARY KEY, hostname TEXT NOT NULL,"
            " transport TEXT NOT NULL, model TEXT, verified_at REAL NOT NULL,"
            " expires_at REAL NOT NULL, score_ip REAL NOT NULL DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO warm_pool(ip, hostname, transport, model, verified_at,"
            " expires_at, score_ip) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT ip FROM warm_pool ORDER BY ip").fetchall()
    finally:
        conn.close()


NOW = 1_800_000_000.0


def test_pick_consumes_and_excludes_ips_in_use(tmp_path):
    db = _db(
        tmp_path,
        [
            ("10.0.0.2", "used-host", "wireguard", "", NOW - 10, NOW + 600, 5.0),
            ("10.0.0.3", "free-host", "wireguard", "", NOW - 20, NOW + 600, 1.0),
        ],
    )
    stations = {2: {"current_ip": "10.0.0.2"}}
    picked = _warm_pool_pick(1, stations, NOW, db=db)
    assert picked is not None
    assert picked[1] == "free-host"
    assert _rows(db) == [("10.0.0.2",)], "l'IP consommée est supprimée du stock"


def test_expired_entries_are_ignored(tmp_path):
    db = _db(
        tmp_path,
        [("10.0.0.9", "expired", "wireguard", "", NOW - 10, NOW - 1, 9.0)],
    )
    assert _warm_pool_pick(1, {}, NOW, db=db) is None


def test_fresh_entries_preferred_over_stale(tmp_path):
    db = _db(
        tmp_path,
        [
            ("10.0.0.4", "stale", "wireguard", "", NOW - 7200, NOW + 600, 99.0),
            ("10.0.0.5", "fresh", "wireguard", "", NOW - 30, NOW + 600, 0.0),
        ],
    )
    picked = _warm_pool_pick(1, {}, NOW, db=db)
    assert picked is not None and picked[1] == "fresh"


def test_transport_mismatch_is_skipped(tmp_path):
    db = _db(
        tmp_path,
        [("10.0.0.6", "tcp-host", "openvpn-tcp", "", NOW - 5, NOW + 600, 9.0)],
    )
    assert _warm_pool_pick(1, {}, NOW, transport="wireguard", db=db) is None
    picked = _warm_pool_pick(1, {}, NOW, transport="openvpn-tcp", db=db)
    assert picked is not None and picked[1] == "tcp-host"


def test_empty_pool_returns_none(tmp_path):
    db = _db(tmp_path, [])
    assert _warm_pool_pick(1, {}, NOW, db=db) is None
