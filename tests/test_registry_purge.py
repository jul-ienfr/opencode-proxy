"""019 T304 — purge registre, checkpoint WAL et integrity_check."""

from __future__ import annotations

import sqlite3
import time

from free.qualification.store import QualificationStore
from scripts.registry_purge import purge_once


def _seed(db, *, now, retention_days=7.0):
    store = QualificationStore(db, retention_days=retention_days)
    store.close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS warm_pool ("
        "ip TEXT PRIMARY KEY, hostname TEXT NOT NULL, transport TEXT NOT NULL, "
        "model TEXT, verified_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "score_ip REAL NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS claims ("
        "hostname TEXT PRIMARY KEY, station INTEGER NOT NULL, status TEXT NOT NULL, "
        "egress_ip TEXT, retry_after REAL NOT NULL DEFAULT 0, reset_at REAL NOT NULL DEFAULT 0, "
        "error TEXT, updated_at REAL NOT NULL, lease_until REAL NOT NULL, "
        "fail_count INTEGER NOT NULL DEFAULT 0)"
    )
    old = now - (retention_days + 2) * 86400
    for i in range(1000):
        conn.execute(
            "INSERT OR IGNORE INTO warm_pool(ip,hostname,transport,model,verified_at,expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"10.0.{i // 255}.{i % 255}", "h", "t", None, old, old),
        )
    # Un warm_pool encore valide doit survivre.
    conn.execute(
        "INSERT OR REPLACE INTO warm_pool(ip,hostname,transport,model,verified_at,expires_at) "
        "VALUES ('192.0.2.1','h','t',NULL,?,?)",
        (now, now + 600),
    )
    conn.execute(
        "INSERT OR REPLACE INTO claims(hostname,station,status,updated_at,lease_until) "
        "VALUES ('old-429',1,'429',?,?)",
        (old, old),
    )
    conn.execute(
        "INSERT OR REPLACE INTO claims(hostname,station,status,updated_at,lease_until) "
        "VALUES ('active',1,'claimed',?,?)",
        (old, now + 600),
    )
    conn.execute(
        "INSERT INTO incidents(scope,station,started_at,ended_at,cause) VALUES ('station',1,?,?,'x')",
        (old, old),
    )
    conn.commit()
    conn.close()


def test_purge_removes_expired_warm_pool_and_old_claims(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    _seed(db, now=now)
    report = purge_once(db, now=now)
    assert report["warm_pool"] == 1000
    assert report["claims"] == 1
    assert report["history"] >= 1
    assert report["integrity"].lower() == "ok"

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM warm_pool").fetchone()[0] == 1
        remaining = {r[0] for r in conn.execute("SELECT hostname FROM claims")}
        assert remaining == {"active"}
        assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
    finally:
        conn.close()


def test_purge_is_idempotent(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    _seed(db, now=now)
    first = purge_once(db, now=now)
    second = purge_once(db, now=now)
    assert first["warm_pool"] == 1000
    assert second["warm_pool"] == 0
    assert second["claims"] == 0
    assert second["integrity"].lower() == "ok"


def test_checkpoint_when_wal_is_large(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    _seed(db, now=now)
    # Force un WAL non vide (écriture sans checkpoint) — le handle reste ouvert.
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    for i in range(500):
        conn.execute(
            "INSERT OR REPLACE INTO claims(hostname,station,status,updated_at,lease_until) "
            "VALUES (?,1,'429',?,?)",
            (f"w{i}", now - 10 * 86400, now),
        )
    conn.commit()
    try:
        report = purge_once(db, now=now, wal_max_bytes=1)
    finally:
        conn.close()
    assert report["wal_checkpointed"] is True
    assert report["integrity"].lower() == "ok"


def test_separate_claims_db_is_purged(tmp_path):
    registry = tmp_path / "registry.db"
    claims = tmp_path / "claims.db"
    now = time.time()
    _seed(registry, now=now)
    # Un claims.db séparé, avec une vieille ligne terminale.
    from free.catalog_claims import CatalogClaims

    store = CatalogClaims(str(claims))
    store.close()
    conn = sqlite3.connect(str(claims))
    conn.execute(
        "INSERT OR REPLACE INTO claims(hostname,station,status,updated_at,lease_until) "
        "VALUES ('old',1,'failed',?,?)",
        (now - 30 * 86400, now),
    )
    conn.commit()
    conn.close()
    report = purge_once(registry, claims_db=claims, now=now)
    assert report["claims"] == 1
    conn = sqlite3.connect(str(claims))
    try:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    finally:
        conn.close()
