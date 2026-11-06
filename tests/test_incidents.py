"""Fleet incident journal (T051)."""

from __future__ import annotations

import sqlite3

from observability.incidents import open_incidents, sync_incidents
from rotation.infrastructure.db.migrate import apply_schema, connect

NOW = 1_793_000_000.0


def _db(tmp_path):
    path = tmp_path / "registry.db"
    conn = connect(path)
    apply_schema(conn, now=NOW)
    conn.close()
    return path


def test_incidents_open_deduplicate_and_close(tmp_path):
    db = _db(tmp_path)
    alerts = [
        {
            "scope": "fleet",
            "cause": "client_usable_low",
            "remediation": "repair",
            "details": {"usable": 0},
        },
        {
            "scope": "fleet",
            "cause": "warm_pool_low",
            "details": {"warm_pool": 0},
        },
    ]
    first = sync_incidents(alerts, path=db, now=NOW)
    assert first["opened"] == 2
    assert first["open"] == 2

    second = sync_incidents(alerts, path=db, now=NOW + 10)
    assert second["opened"] == 0
    assert second["closed"] == 0
    assert len(open_incidents(path=db)) == 2

    cleared = sync_incidents(alerts[:1], path=db, now=NOW + 20)
    assert cleared["closed"] == 1
    assert [row["cause"] for row in open_incidents(path=db)] == [
        "client_usable_low"
    ]

    sync_incidents([], path=db, now=NOW + 30)
    assert open_incidents(path=db) == []
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE ended_at IS NOT NULL"
        ).fetchone()[0] == 2
    finally:
        conn.close()
