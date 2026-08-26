"""
app.db — SQLite WAL (requests.db batch Queue 10000/batch32/timeout50ms)

Extraction de opencode.py: _conn, _db_commit_lock, PRAGMA busy_timeout 5000 / cache_size 64000 / mmap 268M / WAL+NORMAL,
wal_checkpoint_interval 3600, queue batch. DI via app.state / _db_path.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DB_PATH = os.path.join(ROOT, "logs", "requests.db")

# L'impl reste dans opencode.py pour l'instant (import circulaire)
# Prochaine PR: déplacer _conn + _persist + _batch logic ici.

# ── [P5 préparation] Inventaire d'extraction (tranche 1 : noyau DB) ──────
# Périmètre candidat, ancré au 2026-08-26 (opencode.py) :
#   * Connexion/schema : _db_path/_conn (~765), PRAGMAs + CREATE TABLE
#     requests + index (idx_timestamp/model/success/account/ts_model/
#     free_ip/original_model) (~765-884).
#   * Sérialisation : locks (_db_commit_lock ~885), queue batch
#     (_db_queue maxsize=10000, batch 32, flush 50 ms) (~887-921,
#     1176-1198, 1338-1351), thread writer _db_writer_loop.
#   * Lignes : _DbRowRaw + _materialize_db_row (+ registre
#     _tools_used_seen consommé par dashboard/api tools_provider).
#   * Maintenance : _db_maintenance_loop (purge rétention +
#     wal_checkpoint_interval) (~2538).
#
# Seams de test à préserver (re-export obligatoire depuis opencode.py) :
#   oc._quick_body_size, oc._DB_RAW_SIZE_CAP, oc._DbRowRaw,
#   oc._materialize_db_row, oc._db_execute_batch_sync
#   (tests/test_db_offload.py les consomme directement).
#
# Points de DI pour casser les cycles (aucun import opencode ici) :
#   init(db_path, *, redact_fn, truncate_fn, yaml_get_fn) — _redact et
#   _truncate_body_for_storage restent la propriété d'opencode.py et sont
#   injectés au boot ; _materialize_db_row les résout via l'état module.
#   dashboard/api reçoit déjà tools_provider en kwarg — inchangé.
#
# Ordre de la série de PR (une seule extraction à la fois, suite verte
# ~740+ tests entre chacune — cf. plan audit-perf-qualité Phase 5) :
#   1. noyau DB (ce périmètre) → app/db/__init__.py
#   2. conversion pure → app/protocol/__init__.py
#   3. routage/résolution modèle → app/router/__init__.py
#   4. quotas free-model → app/quotas/__init__.py
#   5. streaming SSE → app/streaming/__init__.py

__all__ = ["DB_PATH"]
