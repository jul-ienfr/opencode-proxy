"""Tests de la réserve client (022 T001/T002/T003)."""

from __future__ import annotations

from pathlib import Path

from free import client_reserve as cr


def test_add_list_remove(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    assert cr.add("1.2.3.4", "host.nordvpn.com", 1, db=db)
    assert cr.reserved_ips(db=db) == {"1.2.3.4"}
    assert cr.remove("1.2.3.4", db=db) == 1
    assert cr.reserved_ips(db=db) == set()


def test_expired_not_listed(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    cr.add("1.2.3.4", "host.nordvpn.com", 1, ttl_s=60, now=1000, db=db)
    assert cr.reserved_ips(now=1001, db=db) == {"1.2.3.4"}
    assert cr.reserved_ips(now=2000, db=db) == set()


def test_prune_removes_expired(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    cr.add("1.2.3.4", "host.nordvpn.com", 1, ttl_s=60, now=1000, db=db)
    assert cr.prune(now=2000, db=db) == 1
    assert cr.reserved_ips(now=2000, db=db) == set()


def test_add_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    assert cr.add("1.2.3.4", "a.nordvpn.com", 1, db=db)
    assert cr.add("1.2.3.4", "b.nordvpn.com", 2, db=db)
    assert cr.reserved_ips(db=db) == {"1.2.3.4"}
