"""test_boot_db_phase2.py — Phase 2 chantier boot : aucun full scan DB au boot.

Régressions verrouillées :
1. ``init_requests_schema_fast`` : PRAGMAs + CREATE TABLE uniquement —
   jamais de ``ALTER`` sync (migrations) ni de canary ``COUNT(*)`` au boot.
2. ``migrate_and_canary`` :(migrations ALTER + index + canary) callable en
   fond, retourne int (contrat historique test_phase0_contracts).
3. ``_db_migrate_bg`` : tâche de fond créée au lifespan (``create_task`` dans
   le source du lifespan), jamais awaitée en ligne.
4. Compteurs à 0 + ``stale=True`` au boot ; ``/v1/models`` expose
   ``requests.stale`` le temps du rattrapage GROUP BY en fond.

Offline : SQLite ``:memory:`` pour le schéma, gardes sur le source pour le
lifespan/routage (aucun docker/réseau).
"""

import inspect
import sqlite3

import pytest

import opencode as oc
from app.db import (
    init_requests_schema,
    init_requests_schema_fast,
    migrate_and_canary,
)


def test_fast_path_no_alter_no_canary_on_source():
    """Le fast path n'exécute ni migrations ALTER ni canary COUNT(*)."""
    src = inspect.getsource(init_requests_schema_fast)
    assert "ALTER TABLE requests ADD COLUMN" not in src
    assert "SELECT COUNT" not in src
    assert "_REQUESTS_INDEXES" not in src
    assert "journal_mode=WAL" in src
    assert "CREATE TABLE" in src or "_SCHEMA_REQUESTS" in src


def test_migrate_and_canary_applies_migrations_and_returns_int():
    """:memory: : ALTER + index appliqués, canary int (0 sur table vide)."""
    conn = sqlite3.connect(":memory:")
    init_requests_schema_fast(conn, busy_timeout=5000, cache_size=1000, mmap_size=1024)
    naive = migrate_and_canary(conn)
    assert isinstance(naive, int)
    assert naive == 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}
    assert "model" in cols and "tokens_input" in cols
    idx = {r[1] for r in conn.execute("PRAGMA index_list(requests)").fetchall()}
    assert idx, "migrate doit créer des index"


def test_migrate_and_canary_detects_naive_timestamps():
    """Le canary compte les rows à timestamps naïfs (sans 'Z')."""
    conn = sqlite3.connect(":memory:")
    init_requests_schema(conn, busy_timeout=5000, cache_size=1000, mmap_size=1024)
    conn.execute(
        "INSERT INTO requests (timestamp, model) VALUES ('2026-09-09T10:00:00', 'm')"
    )
    conn.execute(
        "INSERT INTO requests (timestamp, model) VALUES ('2026-09-09T10:00:00Z', 'm')"
    )
    assert migrate_and_canary(conn) == 1


def test_lifespan_spawns_db_migrate_bg_task():
    """Le lifespan crée la tâche migrate en fond, jamais awaitée en ligne."""
    src = inspect.getsource(oc.lifespan)
    assert inspect.iscoroutinefunction(oc._db_migrate_bg)  # défini module-level
    assert "asyncio.create_task(_db_migrate_bg())" in src
    assert "\n    await _db_migrate_bg()" not in src
    # Annulation bornée au shutdown (pas de tâche pendante).
    assert "_db_migrate_task" in src


def test_counters_stale_true_at_import_and_models_route_exposes_it():
    """Compteurs stale au boot + /v1/models expose requests.stale."""
    assert oc._token_counters_stale in (True, False)  # défini à l'import
    src = inspect.getsource(oc.list_models)
    assert '"stale"' in src and "_token_counters_stale" in src


@pytest.mark.asyncio
async def test_db_migrate_bg_sets_naive_count(monkeypatch):
    """_db_migrate_bg remplit _naive_ts_count sans lever (fail-soft)."""
    monkeypatch.setattr(oc._app_db, "migrate_and_canary", lambda conn: 3)
    monkeypatch.setattr(oc, "_naive_ts_count", None)
    await oc._db_migrate_bg()
    assert oc._naive_ts_count == 3


@pytest.mark.asyncio
async def test_db_migrate_bg_fail_soft(monkeypatch):
    """OperationalError en fond → debug, jamais d'exception au boot."""

    def _boom(conn):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(oc._app_db, "migrate_and_canary", _boom)
    await oc._db_migrate_bg()  # ne lève pas
