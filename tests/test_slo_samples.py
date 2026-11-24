"""019 T302/T313 — échantillons SLO minute et preuve « 6/6 même cycle »."""

from __future__ import annotations

import sqlite3

from free.qualification import slo_samples as S


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    S.ensure_schema(conn)
    return conn


def test_record_and_latest_sample_round_trip():
    conn = _conn()
    S.record_sample(
        conn,
        sampled_at=1000.0,
        stations_client_usable=6,
        warm_pool_live=4,
        ip_200_rate=0.75,
        rotate_latency_seconds=42.5,
        incidents_open=1,
        rotation_jobs_orphans=2,
    )
    rows = S.latest_samples(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row.sampled_at == 1000.0
    assert row.stations_client_usable == 6
    assert row.warm_pool_live == 4
    assert row.ip_200_rate == 0.75
    assert row.rotate_latency_seconds == 42.5
    assert row.incidents_open == 1
    assert row.rotation_jobs_orphans == 2


def test_six_of_six_requires_a_non_empty_window_below_the_threshold():
    conn = _conn()
    assert S.six_of_six_held(conn, now=1000.0) is False, "sans mesure on ne prouve rien"
    S.record_sample(conn, sampled_at=990.0, stations_client_usable=6)
    S.record_sample(conn, sampled_at=980.0, stations_client_usable=5)
    assert S.six_of_six_held(conn, window_s=60.0, now=1000.0) is False
    S.record_sample(conn, sampled_at=980.0, stations_client_usable=6)
    assert S.six_of_six_held(conn, window_s=60.0, now=1000.0) is True


def test_sample_once_reads_the_registry_and_tolerates_missing_tables():
    conn = _conn()
    s = S.sample_once(conn, now=2000.0)
    assert s.stations_client_usable == 0
    assert s.warm_pool_live == 0
    assert s.incidents_open == 0
    # la ligne est bien écrite malgré les tables absentes
    assert len(S.latest_samples(conn)) == 1


def test_sample_once_counts_usable_stations_warm_pool_and_incidents():
    conn = _conn()
    conn.execute(
        "CREATE TABLE station_state(station INTEGER PRIMARY KEY, client_usable INTEGER)"
    )
    conn.executemany(
        "INSERT INTO station_state VALUES(?,?)",
        [(1, 1), (2, 1), (3, 0), (4, 1), (5, 1), (6, 1)],
    )
    conn.execute(
        "CREATE TABLE warm_pool(ip TEXT PRIMARY KEY, expires_at REAL)"
    )
    conn.executemany(
        "INSERT INTO warm_pool VALUES(?,?)",
        [("1.1.1.1", 3000.0), ("2.2.2.2", 1000.0)],
    )
    conn.execute(
        "CREATE TABLE incidents(id INTEGER PRIMARY KEY, ended_at REAL)"
    )
    conn.executemany("INSERT INTO incidents VALUES(?,?)", [(1, None), (2, None), (3, 50.0)])
    s = S.sample_once(conn, now=2000.0)
    assert s.stations_client_usable == 5
    assert s.warm_pool_live == 1, "seul le warm pool non expiré compte"
    assert s.incidents_open == 2
