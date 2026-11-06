"""End-to-end quota-reset recovery on a registry.db copy."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from free.catalog_claims import CatalogClaims
from free.ip_registry.registry_db import reconcile
from free.quota_ledger import QuotaLedger, utc_day
from rotation.infrastructure.db.migrate import apply_schema, connect

NOW = 1_793_000_000.0
RESET = NOW + 3600.0
ROOT = Path(__file__).resolve().parent.parent


def _post_reset_module():
    path = ROOT / "scripts" / "post_reset_check.py"
    spec = importlib.util.spec_from_file_location("post_reset_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path):
    path = tmp_path / "registry.db"
    conn = connect(path)
    apply_schema(conn, now=NOW)
    conn.execute(
        "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, last_429_at, "
        "reset_at, state) VALUES('1.1.1.1', ?, ?, ?, ?, 'exhausted')",
        (NOW, NOW, NOW, RESET),
    )
    conn.execute(
        "INSERT INTO quota_buckets(ip, model, day, bucket, used, last_status, "
        "reset_at, source, first_seen_at, last_seen_at) "
        "VALUES('1.1.1.1','m',?,'default',1,429,?, 'observed', ?, ?)",
        (utc_day(NOW), RESET, NOW, NOW),
    )
    conn.commit()
    conn.close()
    return path


def test_reset_revives_ip_quota_and_claim(tmp_path):
    db = _db(tmp_path)
    claims = CatalogClaims(str(db), clock=lambda: NOW)
    claims.seed([{"host": "a.nordvpn.com", "country": "Germany"}])
    claimed = claims.claim_next(1, now=NOW)
    claims.record(
        claimed["host"], 1, "429", ip="1.1.1.1", retry_after=RESET - NOW, now=NOW
    )
    assert claims.claim_next(1, now=NOW + 1) is None

    # Before reset: the IP is still exhausted.
    ledger = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    assert ledger.is_exhausted("1.1.1.1", "m") is True

    # Midnight passes: reconcile revives the egress, the ledger expires, and
    # the claims row becomes eligible again.
    result = reconcile(now=RESET + 1, db_path=db)
    assert result["revived_ips"] == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT state FROM egress_ips WHERE ip='1.1.1.1'"
        ).fetchone()[0] == "fresh"
    finally:
        conn.close()
    after = QuotaLedger(path=str(db), persist=True, clock=lambda: RESET + 1)
    assert after.is_exhausted("1.1.1.1", "m") is False
    assert claims.claim_next(1, now=RESET + 1)["host"] == "a.nordvpn.com"


def test_post_reset_check_finds_the_earliest_future_reset(tmp_path):
    module = _post_reset_module()
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO egress_ips(ip, first_seen_at, last_seen_at, "
            "last_429_at, reset_at, state) VALUES('2.2.2.2', ?, ?, ?, ?, 'exhausted')",
            (NOW, NOW, NOW, NOW - 10),
        )
        conn.commit()
    finally:
        conn.close()
    assert module.next_reset_at(db, now=NOW) == RESET
