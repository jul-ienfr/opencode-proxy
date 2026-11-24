"""T105 / T310 — consume warm pool before qualification.

The warm pool is a cache of egresses that already produced a real 200.
Assignment must consume it before launching a qualification sweep, so a
healthy spare is never wasted on a blind probe. A stale spare is re-probed,
never served. An empty/unavailable pool falls through to the existing
qualification path unchanged (fail-open).
"""

from __future__ import annotations

import sqlite3

import free.pool as pool_mod
from scripts import fresh_chain_supervisor as supervisor

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE warm_pool ("
        "  ip TEXT PRIMARY KEY, hostname TEXT, transport TEXT, model TEXT,"
        "  verified_at REAL, expires_at REAL, score REAL, score_breakdown TEXT"
        ")"
    )
    conn.commit()
    conn.close()
    return db


def _insert(db, *, ip, hostname="h.example", transport="wireguard", model="m",
            verified_at, expires_at, score=1.0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO warm_pool(ip,hostname,transport,model,verified_at,"
        "expires_at,score,score_breakdown) VALUES (?,?,?,?,?,?,?,?)",
        (ip, hostname, transport, model, verified_at, expires_at, score, "{}"),
    )
    conn.commit()
    conn.close()


def _count(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM warm_pool").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# consume_warm_pool_entry (free.pool) — the cache primitive
# ---------------------------------------------------------------------------


def test_serve_picks_fresh_live_entry_and_deletes_it(tmp_path):
    db = _make_db(tmp_path)
    now = 1_000_000.0
    _insert(db, ip="1.2.3.4", hostname="fr1.nord", verified_at=now - 60,
            expires_at=now + 3600)

    res = pool_mod.consume_warm_pool_entry(db, now=now, max_age_s=3600)

    assert res["action"] == "serve"
    assert res["ip"] == "1.2.3.4"
    assert res["hostname"] == "fr1.nord"
    assert res["age_s"] == 60
    assert _count(db) == 0, "served entry must be consumed (deleted)"


def test_fresh_entry_served_stale_reprobed(tmp_path):
    """A fresh proof is served; an old proof is re-probed, never served."""
    db = _make_db(tmp_path)
    now = 1_000_000.0
    _insert(db, ip="1.1.1.1", hostname="fresh", verified_at=now - 120,
            expires_at=now + 3600)
    res_fresh = pool_mod.consume_warm_pool_entry(db, now=now, max_age_s=3600)
    assert res_fresh["action"] == "serve"
    assert res_fresh["ip"] == "1.1.1.1"

    _insert(db, ip="2.2.2.2", hostname="stale", verified_at=now - 7200,
            expires_at=now + 3600)
    res_stale = pool_mod.consume_warm_pool_entry(db, now=now, max_age_s=3600)
    assert res_stale["action"] == "reprobe"
    assert res_stale["ip"] == "2.2.2.2"
    assert res_stale["age_s"] == 7200
    assert _count(db) == 0, "stale entry is consumed so it is not re-picked"


def test_stale_entry_is_never_served_as_fresh(tmp_path):
    db = _make_db(tmp_path)
    now = 1_000_000.0
    _insert(db, ip="9.9.9.9", verified_at=now - 3601, expires_at=now + 5000)

    res = pool_mod.consume_warm_pool_entry(db, now=now, max_age_s=3600)

    assert res["action"] == "reprobe"
    assert res["ip"] == "9.9.9.9"
    assert _count(db) == 0


def test_expired_entry_is_ignored_empty(tmp_path):
    db = _make_db(tmp_path)
    now = 1_000_000.0
    _insert(db, ip="3.3.3.3", verified_at=now - 10, expires_at=now - 1)

    res = pool_mod.consume_warm_pool_entry(db, now=now, max_age_s=3600)

    assert res["action"] == "empty"
    assert _count(db) == 1, "expired row is not a live candidate; leave it alone"


def test_empty_pool_returns_empty(tmp_path):
    db = _make_db(tmp_path)
    assert pool_mod.consume_warm_pool_entry(db, now=1_000_000.0)["action"] == "empty"


def test_missing_db_returns_empty_fail_open(tmp_path):
    missing = tmp_path / "nope.db"
    assert pool_mod.consume_warm_pool_entry(missing, now=1_000_000.0)["action"] == "empty"


def test_excluded_ip_is_skipped(tmp_path):
    db = _make_db(tmp_path)
    now = 1_000_000.0
    _insert(db, ip="1.1.1.1", verified_at=now - 10, expires_at=now + 3600)
    _insert(db, ip="2.2.2.2", verified_at=now - 20, expires_at=now + 3600)

    res = pool_mod.consume_warm_pool_entry(
        db, now=now, max_age_s=3600, exclude_ips={"1.1.1.1"}
    )

    assert res["action"] == "serve"
    assert res["ip"] == "2.2.2.2"
    assert _count(db) == 1, "excluded live row stays for a later cycle"


# ---------------------------------------------------------------------------
# supervisor gate — consume before qualification, fail-open
# ---------------------------------------------------------------------------


def test_supervisor_consume_wrapper_delegates(tmp_path):
    db = _make_db(tmp_path)
    now = 1_000_000.0
    _insert(db, ip="5.5.5.5", hostname="ok", verified_at=now - 5, expires_at=now + 3600)

    res = supervisor._consume_warm_pool_before_qualification(db, now=now)

    assert res["action"] == "serve"
    assert res["ip"] == "5.5.5.5"


def test_supervisor_consume_wrapper_fail_open_on_error(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(pool_mod, "consume_warm_pool_entry", boom)

    res = supervisor._consume_warm_pool_before_qualification()

    assert res["action"] == "empty"


def test_qualification_due_respects_daemon_config(monkeypatch):
    cfg = {
        ("egress_catalog", "qualification.daemon_enabled"): True,
        ("egress_catalog", "qualification.daemon_interval_s"): 900,
    }

    def fake_yaml_get(*args, **_kwargs):
        key = tuple(args[:2])
        if key in cfg:
            return cfg[key]
        return args[2] if len(args) > 2 else None

    monkeypatch.setattr(supervisor, "yaml_get", fake_yaml_get)
    now = 10_000.0
    assert supervisor._qualification_due(now - 901, now) is True
    assert supervisor._qualification_due(now - 100, now) is False


def test_warm_pool_min_defaults_to_six(monkeypatch):
    monkeypatch.setattr(supervisor, "yaml_get", lambda *_a, **_k: (_ for _ in ()).throw(TypeError()))
    # yaml_get raising bubbles into the try/except -> default
    assert supervisor._warm_pool_min() == supervisor.WARM_POOL_MIN_DEFAULT == 6