"""[019 T314] Compteurs AUTH persistés — un seul écrivain, une passe/minute.

Contrat vérifié sans réseau ni Docker :
- deux tentatives sur la même station dans la même minute -> compteur 2 et
  une seule passe de `_auth_gate` (la seconde voit le compteur et s'abstient) ;
- deux stations différentes dans la même minute -> compteurs indépendants ;
- registre absent -> fail-soft (comportement d'avant T314, aucune exception).
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from vpn.auth import _AUTH_GLOBAL_STATION

ROOT = Path(__file__).resolve().parent.parent


def _supervisor():
    path = ROOT / "scripts" / "fresh_chain_supervisor.py"
    spec = importlib.util.spec_from_file_location(
        "fresh_chain_supervisor", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minute(now: float) -> str:
    """Même bucket UTC que ``vpn.auth._auth_minute``."""
    return datetime.fromtimestamp(float(now), UTC).strftime("%Y-%m-%dT%H:%M")


def _registry(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(path))
    conn.close()
    monkeypatch.setenv("OPENCODE_REGISTRY_DB", str(path))
    return path


def _attempts(path: Path, station: int, minute: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT attempts FROM auth_attempts "
            "WHERE station=? AND minute=?",
            (int(station), minute),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def test_two_attempts_same_station_minute_only_one_gate_pass(
    tmp_path, monkeypatch
):
    module = _supervisor()
    db = _registry(tmp_path, monkeypatch)
    now = 1_000_000.0
    minute = _minute(now)

    # La première chaîne réclame la minute et obtient la passe.
    assert (
        module._auth_gate(now, 0.0, 0.0, auth_recent=False, station=1)
        == ""
    )
    # La seconde voit attempts=2 et s'abstient.
    assert (
        module._auth_gate(now, 0.0, 0.0, auth_recent=False, station=1)
        == "attempts"
    )
    assert _attempts(db, 1, minute) == 2
    assert _attempts(db, _AUTH_GLOBAL_STATION, minute) == 2


def test_two_different_stations_are_independent(tmp_path, monkeypatch):
    module = _supervisor()
    db = _registry(tmp_path, monkeypatch)
    now = 2_000_000.0
    minute = _minute(now)

    assert (
        module._auth_gate(now, 0.0, 0.0, auth_recent=False, station=1)
        == ""
    )
    assert (
        module._auth_gate(now, 0.0, 0.0, auth_recent=False, station=2)
        == ""
    )
    assert _attempts(db, 1, minute) == 1
    assert _attempts(db, 2, minute) == 1
    # Le compteur global, lui, additionne les deux stations.
    assert _attempts(db, _AUTH_GLOBAL_STATION, minute) == 2


def test_registry_missing_is_fail_soft(tmp_path, monkeypatch):
    module = _supervisor()
    monkeypatch.setenv(
        "OPENCODE_REGISTRY_DB", str(tmp_path / "missing-registry.db")
    )
    now = 3_000_000.0

    # Aucun fichier : l'écriture et la lecture rendent 0, sans lever.
    assert module._record_auth_attempt(1, now) == 0
    assert module._auth_attempt_count(1, now) == 0
    # Le gate conserve le comportement d'avant T314 (passe autorisée).
    assert (
        module._auth_gate(now, 0.0, 0.0, auth_recent=False, station=1)
        == ""
    )


def test_record_auth_attempt_is_the_single_writer(tmp_path, monkeypatch):
    module = _supervisor()
    db = _registry(tmp_path, monkeypatch)
    now = 4_000_000.0
    minute = _minute(now)

    assert module._record_auth_attempt(1, now) == 1
    assert module._record_auth_attempt(1, now) == 2
    assert module._auth_attempt_count(1, now) == 2
    assert _attempts(db, 1, minute) == 2
    # La minute suivante rouvre une passe.
    assert module._record_auth_attempt(1, now + 60.0) == 1
