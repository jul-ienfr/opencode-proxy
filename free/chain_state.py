"""Small JSON documents stored in ``registry.db`` (no state JSON on disk).

Historically the chains wrote ``logs/fresh_chain_state_<station>[_<transport>].json``
and the rotation engine wrote ``logs/shared_rotation.json`` /
``logs/latency_engine.json``. Those documents now live in the ``chain_state``
table; existing JSON files are read once and renamed ``.migrated``.

Tests that pass a temporary state path get a temporary ``registry.db`` next to
it, never the production file. Writes fall back to the legacy JSON file only
when SQLite itself is unavailable, so a corrupt registry can never kill the
repair loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_CHAIN_DEFAULT = {"index": 0, "results": [], "winner": None}


def db_path_for(state_path: Path) -> Path:
    state_path = Path(state_path)
    logs = ROOT / "logs"
    if state_path.parent.resolve() == logs.resolve():
        return logs / "registry.db"
    return state_path.parent / "registry.db"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chain_state("
        "namespace TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
    )


def _read_row(db: Path, namespace: str) -> str | None:
    try:
        conn = sqlite3.connect(str(db), timeout=10.0)
        try:
            _ensure(conn)
            row = conn.execute(
                "SELECT value FROM chain_state WHERE namespace=?",
                (namespace,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return str(row[0]) if row is not None else None


def _write_row(db: Path, namespace: str, value: Any) -> bool:
    payload = json.dumps(value, separators=(",", ":"))
    try:
        conn = sqlite3.connect(str(db), timeout=10.0)
        try:
            _ensure(conn)
            conn.execute(
                "INSERT INTO chain_state(namespace, value, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(namespace) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (namespace, payload, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def _write_json_fallback(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json_state(path: Path, default: Any = None) -> Any:
    """Load a namespace document, migrating its legacy JSON file once.

    Returns ``default`` when neither the registry row nor a readable legacy
    file exists. A corrupt legacy file is left in place and treated as
    missing (fail-open), matching the previous rotation behavior.
    """

    path = Path(path)
    db = db_path_for(path)
    namespace = path.stem
    raw = _read_row(db, namespace)
    if raw is not None:
        try:
            return json.loads(raw)
        except Exception:
            return default
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default
    if _write_row(db, namespace, value):
        try:
            path.replace(path.with_name(path.name + ".migrated"))
        except OSError:
            pass
    return value


def save_json_state(path: Path, value: Any) -> bool:
    """Persist a namespace document. Returns True when registry.db was used."""

    path = Path(path)
    db = db_path_for(path)
    namespace = path.stem
    if _write_row(db, namespace, value):
        return True
    try:
        _write_json_fallback(path, value)
    except Exception:
        pass
    return False


def load_state(path: Path) -> dict:
    state = load_json_state(path, default=None)
    if not isinstance(state, dict):
        return dict(_CHAIN_DEFAULT)
    for key, value in _CHAIN_DEFAULT.items():
        state.setdefault(key, value)
    return state


def save_state(path: Path, state: dict) -> None:
    save_json_state(path, state)
