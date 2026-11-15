"""Contrats zéro 429 : réserve, budget et exclusion des IPs épuisées (022)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from free import client_reserve as cr


def _db_with_reserved(tmp_path: Path) -> Path:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE quota_buckets("
        "ip TEXT, last_status INTEGER, reset_at REAL, used INTEGER)"
    )
    conn.commit()
    conn.close()
    assert cr.add("1.1.1.1", "host.nordvpn.com", 1, db=db)
    return db


def _bucket(db: Path, status: int, reset: float, used: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO quota_buckets(ip, last_status, reset_at, used) "
        "VALUES('1.1.1.1',?,?,?)",
        (status, reset, used),
    )
    conn.commit()
    conn.close()


def _poisoned_bucket(
    db: Path, status: int, seen: float, error: str = "probe-format-poisoned"
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "ALTER TABLE quota_buckets ADD COLUMN last_error TEXT"
    )
    conn.execute(
        "ALTER TABLE quota_buckets ADD COLUMN last_seen_at REAL"
    )
    conn.execute(
        "INSERT INTO quota_buckets"
        "(ip, last_status, reset_at, used, last_error, last_seen_at) "
        "VALUES('1.1.1.1',?,0,1,?,?)",
        (status, error, seen),
    )
    conn.commit()
    conn.close()


def test_excludes_ip_429(tmp_path: Path) -> None:
    db = _db_with_reserved(tmp_path)
    _bucket(db, 429, 9_999_999_999.0, 1)
    assert cr.reserved_ips(db=db) == set()


def test_excludes_ip_at_client_budget(tmp_path: Path) -> None:
    db = _db_with_reserved(tmp_path)
    _bucket(db, 200, 0.0, 170)
    assert cr.reserved_ips(db=db) == set()


def test_keeps_fresh_ip(tmp_path: Path) -> None:
    db = _db_with_reserved(tmp_path)
    _bucket(db, 200, 0.0, 10)
    assert cr.reserved_ips(db=db) == {"1.1.1.1"}


def test_excludes_ip_poisoned_by_bad_probe(tmp_path: Path) -> None:
    db = _db_with_reserved(tmp_path)
    _poisoned_bucket(db, 429, seen=1_000.0)
    assert cr.reserved_ips(now=2_000.0, db=db) == set()


def test_keeps_ip_recovered_after_poison(tmp_path: Path) -> None:
    db = _db_with_reserved(tmp_path)
    _poisoned_bucket(db, 200, seen=1_000.0)
    assert cr.reserved_ips(now=2_000.0, db=db) == {"1.1.1.1"}


def test_keeps_ip_when_poison_is_older_than_24h(tmp_path: Path) -> None:
    db = _db_with_reserved(tmp_path)
    _poisoned_bucket(db, 429, seen=1_000.0)
    assert cr.reserved_ips(now=1_000.0 + cr.POISON_TTL_S + 1, db=db) == {
        "1.1.1.1"
    }
