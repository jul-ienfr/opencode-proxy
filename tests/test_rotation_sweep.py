"""Persistent SweepCursor tests (SQLite, no network)."""

from __future__ import annotations

from rotation.application.sweep import SweepCursor
from rotation.infrastructure.db.migrate import apply_schema, connect
from rotation.infrastructure.db.repositories import UnitOfWork


def _schema(tmp_path):
    conn = connect(tmp_path / "registry.db")
    apply_schema(conn, now=1000.0)
    conn.close()


def test_cursor_resumes_after_reopen_and_honours_backoff(tmp_path):
    _schema(tmp_path)
    path = tmp_path / "registry.db"
    with UnitOfWork(path) as uow:
        cursor = SweepCursor(uow.repo)
        cursor.record(
            batch_id="b1",
            country="Germany",
            hostname="a.nordvpn.com",
            transport="wireguard",
            status="no_egress",
            next_eligible_at=150.0,
            tested_at=100.0,
        )
        cursor.record(
            batch_id="b1",
            country="France",
            hostname="b.nordvpn.com",
            transport="wireguard",
            status="fresh",
            next_eligible_at=200.0,
            tested_at=100.0,
        )
        assert cursor.next("b1", now=120.0) is None
        first = cursor.next("b1", now=160.0)
        assert first["hostname"] == "a.nordvpn.com"
        # The caller records the outcome before asking for the next item.
        cursor.record(
            batch_id="b1",
            country="Germany",
            hostname="a.nordvpn.com",
            transport="wireguard",
            status="ok",
            next_eligible_at=500.0,
            tested_at=160.0,
        )
        assert cursor.progress("b1").counts == {"ok": 1, "fresh": 1}

    # Reopen: the cursor is in registry.db, not process memory.
    with UnitOfWork(path) as uow:
        cursor = SweepCursor(uow.repo)
        second = cursor.next("b1", now=250.0)
        assert second["hostname"] == "b.nordvpn.com"
        assert cursor.next("b1", now=400.0)["hostname"] == "b.nordvpn.com"
