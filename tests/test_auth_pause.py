"""Un seul écrivain de la pause AUTH — feature 019, tâche T314."""

from __future__ import annotations

import sqlite3
import threading

from observability.auth_pause import (
    active_pause,
    ensure_schema,
    note_rejection,
    station_counts,
)

NOW = 1_800_000_000.0


def _db(tmp_path):
    path = str(tmp_path / "registry.db")
    ensure_schema(path)
    return path


def _pause_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM auth_pauses").fetchone()[0]
    finally:
        conn.close()


def test_two_rejections_in_one_minute_arm_exactly_one_pause(tmp_path):
    path = _db(tmp_path)
    first = note_rejection(path, station=1, now=NOW, window_s=60, pause_s=1800)
    assert first["pause"] is False and first["writer"] is False
    second = note_rejection(path, station=2, now=NOW + 5, window_s=60, pause_s=1800)
    assert second["pause"] is True and second["writer"] is True
    assert second["until"] == NOW + 5 + 1800
    assert _pause_rows(path) == 1

    third = note_rejection(path, station=3, now=NOW + 10, window_s=60, pause_s=1800)
    assert third["pause"] is True and third["writer"] is False
    assert third["until"] == second["until"], "la pause existante n'est pas réécrite"
    assert _pause_rows(path) == 1, "un seul écrivain, une seule ligne"


def test_simultaneous_chains_elect_one_writer(tmp_path):
    path = _db(tmp_path)
    barrier = threading.Barrier(4)
    results: list[dict] = []
    lock = threading.Lock()

    def chain(station: int) -> None:
        barrier.wait()
        result = note_rejection(
            path, station=station, now=NOW, window_s=60, pause_s=1800
        )
        with lock:
            results.append(result)

    threads = [threading.Thread(target=chain, args=(sid,)) for sid in range(1, 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(results) == 4
    writers = [result for result in results if result["writer"]]
    assert len(writers) == 1, "une seule chaîne arme la pause"
    assert _pause_rows(path) == 1
    assert active_pause(path, now=NOW + 1) == writers[0]["until"]


def test_per_station_and_global_counts_are_in_registry(tmp_path):
    path = _db(tmp_path)
    note_rejection(path, station=1, now=NOW, window_s=600, threshold=99)
    note_rejection(path, station=1, now=NOW + 1, window_s=600, threshold=99)
    note_rejection(path, station=2, now=NOW + 2, window_s=600, threshold=99)
    counts = station_counts(path, now=NOW + 3, window_s=600)
    assert counts == {1: 2, 2: 1}
    assert active_pause(path, now=NOW + 3) == 0.0
