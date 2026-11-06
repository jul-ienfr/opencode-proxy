"""Latency engine persistence now lives in registry.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from free.rotation import COOLDOWN_SOFT, LatencyRotationEngine


def test_engine_state_round_trips_through_registry_db(tmp_path, monkeypatch):
    db = tmp_path / "registry.db"
    state_path = tmp_path / "latency_engine.json"
    engine = LatencyRotationEngine()
    monkeypatch.setattr(engine, "_engine_state_file", lambda: str(state_path))
    monkeypatch.setattr(engine, "_now", lambda: 1000.0)
    engine.cfg.enabled = True
    engine.mark(1, "1.1.1.1", COOLDOWN_SOFT)
    engine._save_engine_state()

    assert not state_path.exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chain_state WHERE namespace='latency_engine'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    other = LatencyRotationEngine()
    monkeypatch.setattr(other, "_engine_state_file", lambda: str(state_path))
    monkeypatch.setattr(other, "_now", lambda: 1000.0)
    other._load_engine_state()
    assert other.cooldown_kind(1, "1.1.1.1") == COOLDOWN_SOFT


def test_engine_migrates_legacy_json_once(tmp_path, monkeypatch):
    state_path = tmp_path / "latency_engine.json"
    state_path.write_text(
        '{"cooldowns":[{"sid":2,"ip":"2.2.2.2","kind":"soft","remaining":30}],'
        '"soft_history":[],"total_soft":1,"total_hard":0}',
        encoding="utf-8",
    )
    engine = LatencyRotationEngine()
    monkeypatch.setattr(engine, "_engine_state_file", lambda: str(state_path))
    monkeypatch.setattr(engine, "_now", lambda: 1000.0)
    engine._load_engine_state()
    assert engine.cooldown_kind(2, "2.2.2.2") == COOLDOWN_SOFT
    assert not state_path.exists()
    assert Path(str(state_path) + ".migrated").exists()
