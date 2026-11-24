"""Pins registry-authoritative — feature 019, tâche T306."""

from __future__ import annotations

import sqlite3

from observability import pins


def test_two_pin_cycles_single_write_and_no_orphan(tmp_path):
    db = str(tmp_path / "registry.db")
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")

    assert pins.set_pin(db, 1, "host-a.example", country="FR", egress_ip="1.2.3.4")
    pins.sync_env(db, env)
    first = env.read_text(encoding="utf-8")

    assert pins.set_pin(db, 1, "host-a.example", country="FR", egress_ip="1.2.3.4") is False, (
        "un cycle identique ne réécrit pas la ligne"
    )
    pins.sync_env(db, env)
    assert env.read_text(encoding="utf-8") == first

    assert pins.set_pin(db, 2, "host-b.example", country="DE")
    pins.sync_env(db, env)
    text = env.read_text(encoding="utf-8")
    assert "SERVER_HOSTNAMES_STATION1=host-a.example" in text
    assert "SERVER_COUNTRIES_STATION1=FR" in text
    assert "SERVER_HOSTNAMES_STATION2=host-b.example" in text
    assert "OTHER=1" in text, "les autres clés .env sont préservées"
    for sid in range(7, 11):
        assert f"SERVER_HOSTNAMES_STATION{sid}=" not in text

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 2
    finally:
        conn.close()


def test_orphan_pins_are_pruned(tmp_path):
    db = str(tmp_path / "registry.db")
    assert pins.set_pin(db, 1, "host-1.example")
    # set_pin refuse > 6 : insertion directe pour simuler l'héritage 7–10.
    conn = sqlite3.connect(db)
    try:
        pins.ensure_schema(conn)
        for sid in (7, 9):
            conn.execute(
                "INSERT OR REPLACE INTO pins(station, hostname, country, egress_ip, source, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, f"legacy-{sid}", "", "", "legacy", 0.0),
            )
        conn.commit()
    finally:
        conn.close()

    assert pins.prune_orphans(db) == [7, 9]
    assert sorted(pins.load_pins(db)) == [1]


def test_migrate_from_env_is_idempotent(tmp_path):
    db = str(tmp_path / "registry.db")
    env = tmp_path / ".env"
    env.write_text(
        "SERVER_HOSTNAMES_STATION1=env-host\nSERVER_COUNTRIES_STATION1=NL\n"
        "SERVER_HOSTNAMES_STATION3=env-host-3\n",
        encoding="utf-8",
    )
    assert pins.migrate_from_env(db, env) == 2
    assert pins.migrate_from_env(db, env) == 0
    rows = pins.load_pins(db)
    assert rows[1]["hostname"] == "env-host" and rows[1]["country"] == "NL"
    assert rows[3]["hostname"] == "env-host-3"
