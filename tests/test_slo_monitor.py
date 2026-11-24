"""Mesure continue 6/6 même cycle — feature 019, tâche T313."""

from __future__ import annotations

import sqlite3

from scripts.slo_monitor import _alert_if_sustained

NOW = 1_800_003_600.0


def _db(tmp_path, rows) -> str:
    path = tmp_path / "registry.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE slo_samples (sampled_at REAL NOT NULL,"
            " station_client_usable INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO slo_samples(sampled_at, station_client_usable)"
            " VALUES (?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


def test_six_of_six_then_five_of_six_alerts_after_ten_minutes(tmp_path):
    rows = [(NOW - 600 + i * 120, 5) for i in range(6)]
    db = _db(tmp_path, rows)
    assert _alert_if_sustained(db, now=NOW - 120) is None, (
        "moins de 10 min de baisse : pas encore d'alerte"
    )
    alert = _alert_if_sustained(db, now=NOW)
    assert alert is not None and "6/6 not reached" in alert


def test_any_six_of_six_sample_clears_the_window(tmp_path):
    rows = [(NOW - 600 + i * 120, 5) for i in range(6)]
    rows[3] = (rows[3][0], 6)
    db = _db(tmp_path, rows)
    assert _alert_if_sustained(db, now=NOW) is None


def test_short_window_does_not_alert(tmp_path):
    rows = [(NOW - 240 + i * 120, 4) for i in range(3)]
    db = _db(tmp_path, rows)
    assert _alert_if_sustained(db, now=NOW) is None
