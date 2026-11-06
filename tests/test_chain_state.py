"""Chain resume state in registry.db (no JSON source of truth)."""

from __future__ import annotations

import json
import sqlite3

from free.chain_state import load_state, save_state


def test_chain_state_round_trips_through_the_registry(tmp_path):
    path = tmp_path / "fresh_chain_state_1_auto.json"
    state = {"index": 12, "winner": {"ip": "1.1.1.1"}, "results": []}
    save_state(path, state)

    assert not path.exists()
    db = tmp_path / "registry.db"
    assert db.exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chain_state").fetchone()[0] == 1
    finally:
        conn.close()
    assert load_state(path) == state


def test_legacy_json_is_imported_once_and_renamed(tmp_path):
    path = tmp_path / "fresh_chain_state_2_auto.json"
    state = {"index": 3, "winner": None, "results": [{"host": "a"}]}
    path.write_text(json.dumps(state), encoding="utf-8")

    assert load_state(path) == state
    assert not path.exists()
    assert (tmp_path / "fresh_chain_state_2_auto.json.migrated").exists()
    assert load_state(path) == state
