"""019 T313 — alerte « 6/6 non tenu » du job SLO."""

from __future__ import annotations

import sqlite3
import time

from free.qualification import slo_samples
from scripts import slo_sample


def _seed(db, *, values):
    conn = sqlite3.connect(str(db))
    try:
        slo_samples.ensure_schema(conn)
        # ``sample_once`` recompte ``station_state`` : on aligne la table sur
        # le dernier échantillon pour que le run CLI produise la même valeur.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS station_state ("
            "station INTEGER PRIMARY KEY, container TEXT, transport_effective TEXT, "
            "state TEXT, country TEXT, current_ip TEXT, current_host TEXT, "
            "client_usable INTEGER NOT NULL DEFAULT 0, last_probe_at REAL, "
            "last_200_at REAL, reason TEXT, updated_at REAL NOT NULL)"
        )
        conn.execute("DELETE FROM station_state")
        for i in range(int(values[-1])):
            conn.execute(
                "INSERT INTO station_state(station,client_usable,updated_at) VALUES(?,?,?)",
                (i + 1, 1, time.time()),
            )
        base = time.time() - len(values) * 60.0
        for i, usable in enumerate(values):
            slo_samples.record_sample(
                conn, sampled_at=base + i * 60.0, stations_client_usable=usable
            )
        conn.commit()
    finally:
        conn.close()


def test_six_of_six_held_by_stored_samples(tmp_path):
    db = tmp_path / "registry.db"
    _seed(db, values=[6, 6, 6, 6, 6, 6])
    conn = sqlite3.connect(str(db))
    try:
        assert slo_samples.six_of_six_held(conn, window_s=10_000) is True
    finally:
        conn.close()


def test_alert_after_one_five_of_six(tmp_path):
    db = tmp_path / "registry.db"
    _seed(db, values=[6, 6, 5, 6, 6, 6])
    conn = sqlite3.connect(str(db))
    try:
        assert slo_samples.six_of_six_held(conn, window_s=10_000) is False
    finally:
        conn.close()


def test_main_check_exits_nonzero_when_alert(tmp_path):
    db = tmp_path / "registry.db"
    _seed(db, values=[6, 5, 6, 6, 6, 6])
    rc = slo_sample.main(["--db", str(db), "--window", "100000", "--check"])
    assert rc == 1


def test_main_check_exits_zero_when_held(tmp_path):
    db = tmp_path / "registry.db"
    _seed(db, values=[6, 6, 6, 6, 6, 6])
    rc = slo_sample.main(["--db", str(db), "--window", "100000", "--check"])
    assert rc == 0
