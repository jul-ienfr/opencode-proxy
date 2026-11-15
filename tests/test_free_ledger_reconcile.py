"""Réconciliation ledger ↔ observations : un 200 réel débloque un 429 (022)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import opencode


class _Ledger:
    def __init__(self, exhausted: bool) -> None:
        self._exhausted = exhausted
        self.calls: list[tuple] = []

    def is_exhausted(self, ip, model) -> bool:
        return self._exhausted

    def note_result(self, ip, model, status, **kwargs) -> None:
        self.calls.append((ip, model, status))


class _Pool:
    def __init__(self, ledger: _Ledger) -> None:
        self._ledger = ledger

    def _get_ledger(self) -> _Ledger:
        return self._ledger


class _Station:
    def __init__(self, ip: str) -> None:
        self.current_ip = ip


def _ledger(exhausted: bool) -> _Ledger:
    return _Ledger(exhausted)


def _db(tmp_path: Path, rows: list[tuple[str, int, float]]) -> Path:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE observations(ip TEXT, status INTEGER, tested_at REAL)"
    )
    conn.executemany(
        "INSERT INTO observations(ip, status, tested_at) VALUES(?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _wire(monkeypatch, ledger: _Ledger, ip: str = "10.0.0.1") -> None:
    import shared_state

    monkeypatch.setattr(shared_state, "free_ip_pool", _Pool(ledger))
    monkeypatch.setattr(shared_state, "vpn_managers", [_Station(ip)])


def test_recent_200_clears_a_future_reset(tmp_path, monkeypatch) -> None:
    now = time.time()
    db = _db(tmp_path, [("10.0.0.1", 200, now - 30)])
    ledger = _ledger(True)
    _wire(monkeypatch, ledger)
    assert opencode._reconcile_free_ledger_once(str(db)) == 1
    assert ledger.calls == [("10.0.0.1", None, 200)]


def test_latest_429_does_not_clear(tmp_path, monkeypatch) -> None:
    now = time.time()
    db = _db(
        tmp_path,
        [("10.0.0.1", 200, now - 60), ("10.0.0.1", 429, now - 5)],
    )
    ledger = _ledger(True)
    _wire(monkeypatch, ledger)
    assert opencode._reconcile_free_ledger_once(str(db)) == 0
    assert ledger.calls == []


def test_stale_200_does_not_clear(tmp_path, monkeypatch) -> None:
    now = time.time()
    db = _db(tmp_path, [("10.0.0.1", 200, now - 3600)])
    ledger = _ledger(True)
    _wire(monkeypatch, ledger)
    assert opencode._reconcile_free_ledger_once(str(db)) == 0
    assert ledger.calls == []


def test_no_future_reset_is_left_alone(tmp_path, monkeypatch) -> None:
    now = time.time()
    db = _db(tmp_path, [("10.0.0.1", 200, now - 30)])
    ledger = _ledger(False)
    _wire(monkeypatch, ledger)
    assert opencode._reconcile_free_ledger_once(str(db)) == 0
    assert ledger.calls == []
