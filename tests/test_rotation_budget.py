"""Tests du budget horaire de rotation WARP persisté (011 T049)."""

from __future__ import annotations

import sqlite3

from free.warp_rotation import RotationBudgetStore


def test_budget_store_persists_and_limits(tmp_path) -> None:
    db = tmp_path / "registry.db"
    store = RotationBudgetStore(str(db))
    assert store.allow("warp-test", 2) is True
    store.record("warp-test")
    store.record("warp-test")
    assert store.allow("warp-test", 2) is False
    assert store.allow("other", 2) is True

    # Un nouveau store sur le même fichier voit les mêmes lignes.
    store2 = RotationBudgetStore(str(db))
    assert store2.allow("warp-test", 2) is False

    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM rotation_jobs WHERE label='warp-test'"
        ).fetchone()[0]
    assert count == 2


def test_budget_store_zero_means_unlimited(tmp_path) -> None:
    store = RotationBudgetStore(str(tmp_path / "registry.db"))
    for _ in range(50):
        store.record("warp-test")
    assert store.allow("warp-test", 0) is True


def test_budget_store_finishes_a_job_with_its_result(tmp_path) -> None:
    db = tmp_path / "registry.db"
    store = RotationBudgetStore(str(db))
    job_id = store.record("warp-1")
    assert job_id is not None
    store.finish(job_id, "ok", "rotated")
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT result, reason, finished_at FROM rotation_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row[0] == "ok"
    assert row[1] == "rotated"
    assert row[2] is not None
