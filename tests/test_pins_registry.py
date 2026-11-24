"""019 T306 — le registre est l'autorité unique des pins hostname."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from free.qualification import pins as P
from scripts import pins_sync


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    P.ensure_schema(conn)
    return conn


def test_upsert_is_idempotent_and_keeps_pinned_at():
    conn = _conn()
    first = P.upsert_pin(conn, 1, "us1.nordvpn.com", country="United States", now=100.0)
    assert first is not None
    second = P.upsert_pin(conn, 1, "us1.nordvpn.com", country="United States", now=200.0)
    assert second is not None
    assert second.pinned_at == 100.0, "même hostname → pinned_at ne bouge pas"
    assert second.updated_at == 200.0
    assert len(P.list_pins(conn)) == 1


def test_changing_hostname_refreshes_pinned_at():
    conn = _conn()
    P.upsert_pin(conn, 3, "kz4.nordvpn.com", now=100.0)
    pin = P.upsert_pin(conn, 3, "kz5.nordvpn.com", now=300.0)
    assert pin is not None and pin.hostname == "kz5.nordvpn.com"
    assert pin.pinned_at == 300.0


def test_empty_hostname_deletes_the_pin():
    conn = _conn()
    P.upsert_pin(conn, 2, "es340.nordvpn.com")
    assert P.upsert_pin(conn, 2, "") is None
    assert P.get_pin(conn, 2) is None


def test_env_migration_is_idempotent_and_exports_station_keys():
    conn = _conn()
    env = {
        "SERVER_HOSTNAMES_STATION1": "us13914.nordvpn.com",
        "SERVER_HOSTNAMES_STATION6": "ca2170.nordvpn.com",
        "SERVER_HOSTNAMES_STATIONX": "ignored",
        "OTHER": "x",
    }
    assert P.migrate_env(conn, env, now=10.0) == 2
    assert P.migrate_env(conn, env, now=20.0) == 0, "2e passage = aucune écriture"
    exported = P.env_hostnames(conn)
    assert exported == {
        "SERVER_HOSTNAMES_STATION1": "us13914.nordvpn.com",
        "SERVER_HOSTNAMES_STATION6": "ca2170.nordvpn.com",
    }
    assert P.get_pin(conn, 6).source == "env-migration"


def test_egress_ip_and_transport_are_preserved():
    conn = _conn()
    P.upsert_pin(
        conn,
        4,
        "hu64.nordvpn.com",
        country="Hungary",
        egress_ip="203.0.113.9",
        transport="openvpn-tcp",
        source="app",
    )
    pin = P.get_pin(conn, 4)
    assert pin is not None
    assert pin.country == "Hungary"
    assert pin.egress_ip == "203.0.113.9"
    assert pin.transport == "openvpn-tcp"
    assert pin.source == "app"


def _ensure_station_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS station_state (
            station INTEGER PRIMARY KEY,
            current_ip TEXT NOT NULL DEFAULT ''
        )
        """
    )


def test_backfill_egress_ip_fills_empty_pins_from_station_state():
    conn = _conn()
    _ensure_station_state(conn)
    for sid in (1, 2, 3, 4):
        P.upsert_pin(conn, sid, f"us{sid}.nordvpn.com", country="US", now=10.0)
        conn.execute(
            "INSERT INTO station_state(station, current_ip) VALUES (?, ?)",
            (sid, f"203.0.113.{sid}"),
        )

    filled = P.backfill_egress_ip(conn, now=20.0)

    assert filled == 4
    for sid in (1, 2, 3, 4):
        pin = P.get_pin(conn, sid)
        assert pin is not None
        assert pin.egress_ip == f"203.0.113.{sid}"
        assert pin.updated_at == 20.0
    assert P.backfill_egress_ip(conn, now=30.0) == 0, "idempotent"


def test_backfill_egress_ip_skips_already_filled_and_missing_state():
    conn = _conn()
    _ensure_station_state(conn)
    P.upsert_pin(conn, 1, "us1.nordvpn.com", egress_ip="198.51.100.1", now=10.0)
    P.upsert_pin(conn, 2, "us2.nordvpn.com", now=10.0)
    P.upsert_pin(conn, 3, "us3.nordvpn.com", now=10.0)
    conn.execute(
        "INSERT INTO station_state(station, current_ip) VALUES (1, '203.0.113.1'),"
        " (2, ''), (3, '203.0.113.3')"
    )

    filled = P.backfill_egress_ip(conn, now=20.0)

    assert filled == 1
    assert P.get_pin(conn, 1).egress_ip == "198.51.100.1"
    assert P.get_pin(conn, 1).updated_at == 10.0
    assert P.get_pin(conn, 2).egress_ip == ""
    assert P.get_pin(conn, 3).egress_ip == "203.0.113.3"


def test_backfill_egress_ip_fail_soft_without_station_state():
    conn = _conn()
    P.upsert_pin(conn, 1, "us1.nordvpn.com", now=10.0)
    assert P.backfill_egress_ip(conn, now=20.0) == 0
    assert P.get_pin(conn, 1).egress_ip == ""


def test_render_env_from_registry_removes_absent_and_adds_present():
    conn = _conn()
    P.upsert_pin(conn, 2, "us2.nordvpn.com", country="US")
    P.upsert_pin(conn, 5, "ca5.nordvpn.com", country="CA")
    env_text = (
        "KEEP=1\n"
        "SERVER_HOSTNAMES_STATION1=old.nordvpn.com\n"
        "SERVER_COUNTRIES_STATION1=Old\n"
        "SERVER_HOSTNAMES_STATION2=stale.nordvpn.com\n"
        "OTHER=ok\n"
    )

    rendered = pins_sync.render_env_from_registry(
        env_text, pins_sync.env_station_lines(conn)
    )

    assert "KEEP=1\n" in rendered
    assert "OTHER=ok\n" in rendered
    assert "SERVER_HOSTNAMES_STATION1=" not in rendered
    assert "SERVER_COUNTRIES_STATION1=" not in rendered
    assert "SERVER_HOSTNAMES_STATION2=us2.nordvpn.com\n" in rendered
    assert "SERVER_COUNTRIES_STATION2=US\n" in rendered
    assert "SERVER_HOSTNAMES_STATION5=ca5.nordvpn.com\n" in rendered
    assert "SERVER_COUNTRIES_STATION5=CA\n" in rendered


def test_write_env_from_registry_writes_file(tmp_path: Path):
    conn = _conn()
    P.upsert_pin(conn, 2, "us2.nordvpn.com", country="US")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "KEEP=1\nSERVER_HOSTNAMES_STATION1=old.nordvpn.com\n",
        encoding="utf-8",
    )

    rendered = pins_sync.write_env_from_registry(env_path, conn)

    assert env_path.read_text(encoding="utf-8") == rendered
    assert "SERVER_HOSTNAMES_STATION1" not in rendered
    assert "SERVER_HOSTNAMES_STATION2=us2.nordvpn.com\n" in rendered
    assert "SERVER_COUNTRIES_STATION2=US\n" in rendered
    assert "KEEP=1\n" in rendered


def test_backfill_egress_ip_and_export_with_pins_sync_script(tmp_path: Path):
    db = tmp_path / "registry.db"
    env_path = tmp_path / ".env"

    seed = sqlite3.connect(db)
    P.ensure_schema(seed)
    _ensure_station_state(seed)
    P.upsert_pin(seed, 1, "us1.nordvpn.com", country="US", now=10.0)
    P.upsert_pin(seed, 2, "us2.nordvpn.com", country="US", now=10.0)
    seed.execute("INSERT INTO station_state(station, current_ip) VALUES (1, '203.0.113.1')")
    seed.commit()
    seed.close()

    env_path.write_text(
        "KEEP=1\nSERVER_HOSTNAMES_STATION9=leak.nordvpn.com\nSERVER_COUNTRIES_STATION9=Leak\n",
        encoding="utf-8",
    )

    assert pins_sync.main(["fill-ip", "--db", str(db), "--env", str(env_path)]) == 0
    assert pins_sync.main(["export", "--write", "--db", str(db), "--env", str(env_path)]) == 0

    after = sqlite3.connect(db)
    after.row_factory = sqlite3.Row
    row = after.execute("SELECT egress_ip FROM pins WHERE station=1").fetchone()
    emptied = after.execute("SELECT egress_ip FROM pins WHERE station=2").fetchone()
    after.close()
    assert row["egress_ip"] == "203.0.113.1"
    assert emptied["egress_ip"] == ""

    text = env_path.read_text(encoding="utf-8")
    assert "KEEP=1\n" in text
    assert "STATION9" not in text
    assert "SERVER_HOSTNAMES_STATION1=us1.nordvpn.com\n" in text
    assert "SERVER_COUNTRIES_STATION1=US\n" in text
    assert "SERVER_HOSTNAMES_STATION2=us2.nordvpn.com\n" in text
