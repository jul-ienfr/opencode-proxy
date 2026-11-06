"""Claims migration into the single registry database."""

from __future__ import annotations

import sqlite3

from free.catalog_claims import CatalogClaims
from rotation.infrastructure.db.maintenance import run_once


def test_claims_are_copied_into_registry_and_remain_usable(tmp_path):
    source = tmp_path / "catalog_claims.db"
    target = tmp_path / "registry.db"
    old = CatalogClaims(str(source))
    old.seed(
        [
            {"host": "a.nordvpn.com", "country": "Germany"},
            {"host": "b.nordvpn.com", "country": "France"},
            {"host": "c.nordvpn.com", "country": "Spain"},
        ]
    )
    first = old.claim_next(1)
    old.record(first["host"], 1, "no_egress", error="no-egress")
    old.close()

    result = run_once(target, backup_dir=None, claims_source=source, now=1000.0)
    assert result["claims"]["catalog"] == 3
    assert result["claims"]["claims"] >= 1

    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] >= 1
    finally:
        conn.close()

    migrated = CatalogClaims(str(target))
    try:
        nxt = migrated.claim_next(2)
        assert nxt["host"] in {"b.nordvpn.com", "c.nordvpn.com"}
    finally:
        migrated.close()
