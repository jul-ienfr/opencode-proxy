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


def test_thousand_client_picks_stay_in_reserve(monkeypatch) -> None:
    """T012 (simulation) : 1 000 sélections, jamais une IP hors réserve."""

    from free import pool as pool_mod

    reserve = {"10.0.0.1", "10.0.0.2"}
    monkeypatch.setattr(
        "free.client_reserve.reserved_ips", lambda **_: set(reserve)
    )
    stations = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
    misses = 0
    for index in range(1000):
        allowed = [ip for ip in stations if pool_mod._reserve_allows(ip)]
        if allowed:
            assert allowed[0] in reserve
        else:
            misses += 1
        if index == 499:
            # Le client consomme la première IP : elle sort de la réserve.
            reserve.discard("10.0.0.1")
    assert misses == 0


def test_hundred_probes_never_touch_the_reserve(monkeypatch) -> None:
    """T013 (simulation) : les probes excluent les IPs réservées."""

    from scripts import fresh_chain

    monkeypatch.setattr(
        fresh_chain, "_ledger_exhausted_ips", lambda api_base: {"10.0.0.9"}
    )
    monkeypatch.setattr(
        fresh_chain,
        "_reserved_client_ips",
        lambda: {"10.0.0.1", "10.0.0.2"},
    )
    monkeypatch.setattr(
        "free.client_reserve.probe_blocked_ips", lambda **_: set()
    )
    excluded = fresh_chain._probe_excluded_ips("http://127.0.0.1:4000")
    assert excluded == {"10.0.0.1", "10.0.0.2", "10.0.0.9"}
    candidates = [f"10.0.0.{i}" for i in range(1, 7)]
    touched = {
        ip
        for _ in range(100)
        for ip in candidates
        if ip not in excluded
    }
    assert not (touched & {"10.0.0.1", "10.0.0.2"})


def test_probe_budget_blocks_half_spent_ip(tmp_path: Path) -> None:
    """T006 : une IP à 50 % de son quota n'est plus sondée."""

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE quota_buckets(ip TEXT, used INTEGER)"
    )
    conn.executemany(
        "INSERT INTO quota_buckets(ip, used) VALUES(?, ?)",
        [("10.0.0.1", cr._probe_budget()), ("10.0.0.2", 40)],
    )
    conn.commit()
    conn.close()
    assert cr.probe_blocked_ips(db=db) == {"10.0.0.1"}


def test_probe_budget_remaining_counts_reserve_only(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE quota_buckets(ip TEXT, used INTEGER)"
    )
    conn.execute("INSERT INTO quota_buckets(ip, used) VALUES('10.0.0.1', 40)")
    conn.commit()
    conn.close()
    assert cr.add("10.0.0.1", "host.nordvpn.com", 1, db=db)
    assert cr.probe_budget_remaining(db=db) == cr._probe_budget() - 40


def test_empty_reserve_leaves_fallback_available(monkeypatch) -> None:
    """T008 : réserve vide = les fallbacks reprennent la main."""

    from free import pool as pool_mod

    monkeypatch.setattr(
        "free.client_reserve.reserved_ips", lambda **_: set()
    )
    assert pool_mod._reserve_allows("10.0.0.99")


def test_quota_error_carries_a_reason(monkeypatch) -> None:
    """T009 : le 429 expose une raison structurée."""

    import opencode

    monkeypatch.setattr(opencode, "_client_429_reason", lambda: "reserve_empty")
    monkeypatch.setattr(opencode, "_CLIENT_429_BY_REASON", {})
    response = opencode._free_preflight_response(42, anthropic=False)
    payload = response.body
    assert b'"reason":"reserve_empty"' in payload
    assert response.headers.get("X-Free-Reason") == "reserve_empty"
    assert response.headers.get("Retry-After") == "42"
    assert opencode._CLIENT_429_BY_REASON == {"reserve_empty": 1}


def test_reserve_survives_a_corrupt_db(tmp_path: Path) -> None:
    """T023 : une base illisible ne fait pas planter le routage."""

    db = tmp_path / "registry.db"
    db.write_bytes(b"not a sqlite database")
    assert cr.reserved_ips(db=db) == set()
    assert cr.count(db=db) == 0


def test_reserve_survives_a_locked_db(tmp_path: Path, monkeypatch) -> None:
    """T023 : une base verrouillée dégrade, elle ne bloque pas 5 s."""

    import sqlite3 as _sqlite

    db = tmp_path / "registry.db"
    assert cr.add("10.0.0.1", "host.nordvpn.com", 1, db=db)
    monkeypatch.setattr(
        cr,
        "_connect",
        lambda target=None: _sqlite.connect(
            str(target or cr.DEFAULT_DB), timeout=0.05
        ),
    )
    locker = _sqlite.connect(str(db), timeout=0.05)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        assert cr.reserved_ips(db=db) == set()
    finally:
        locker.rollback()
        locker.close()
