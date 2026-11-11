"""Runtime registry maintenance: schema, prune, checkpoint, backup."""

from __future__ import annotations

import sqlite3

from rotation.infrastructure.db.maintenance import run_once


def test_maintenance_applies_schema_and_writes_a_restoreable_backup(tmp_path):
    db = tmp_path / "registry.db"
    backups = tmp_path / "backups"
    result = run_once(db, backup_dir=backups, now=1000.0)

    assert result["version"] >= 1
    assert result["integrity_ok"] is True
    assert result["backup"]
    # The exact stamp depends on the local epoch; check the directory instead.
    files = list(backups.glob("registry-*.db"))
    assert len(files) == 1
    conn = sqlite3.connect(files[0])
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert {"schema_migrations", "servers", "egress_ips", "sweep_cursor"} <= tables
    assert result["deleted"]["observations"] == 0


def test_maintenance_is_idempotent_and_keeps_one_backup_per_call(tmp_path):
    db = tmp_path / "registry.db"
    backups = tmp_path / "backups"
    first = run_once(db, backup_dir=backups, now=1000.0)
    second = run_once(db, backup_dir=backups, now=1001.0)
    assert first["version"] == second["version"]
    assert second["integrity_ok"] is True
    assert len(list(backups.glob("registry-*.db"))) == 2
