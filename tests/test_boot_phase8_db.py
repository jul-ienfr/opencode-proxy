"""test_boot_phase8_db.py — agrégat token_counters_daily + archivage (Phase 8).

Deux contrats du plan boot :

1. **Compteurs incrémentaux** : ``_restore_token_counters`` ne fait plus un
   ``GROUP BY`` sur les millions de lignes de ``requests`` mais lit
   ``token_counters_daily`` (agrégat par modèle/jour alimenté par le writer).
   On vérifie que l'agrégat est bien incrémenté par le writer ET que le
   restore rend les mêmes valeurs que l'agrégat historique.
2. **Archivage** : ``scripts/archive_db.py`` déplace l'historique ancien vers
   des archives mensuelles SANS perte (somme conservée), refuse de tourner si
   une instance écrit, et est idempotent.

Offline, sur bases temporaires : aucun accès à ``logs/requests.db`` réel.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from observability import db as odb

ROOT = Path(__file__).resolve().parent.parent


# ── Helpers ──────────────────────────────────────────────────────────


def _requests_table(conn: sqlite3.Connection) -> None:
    """Crée la VRAIE table `requests` (schéma de production complet).

    On rejoue le chemin exact du boot (``init_requests_schema_fast`` puis
    ``migrate_and_canary``) plutôt qu'un sous-ensemble de colonnes : le writer
    insère 32 colonnes dont certaines (``client_ip``, ``station``…) n'existent
    qu'APRÈS migrations. Une table réduite fait échouer l'INSERT, le batch est
    « sauté » et le test ne prouve plus rien (piège rencontré ici).
    """
    odb.init_requests_schema_fast(conn, busy_timeout=5000, cache_size=64000, mmap_size=0)
    odb.migrate_and_canary(conn)


def _insert_request(conn, rid, ts, model="glm-5.1", ti=10, to=5, tc=1):
    """Ligne minimale via le schéma réel (colonnes restantes à NULL)."""
    conn.execute(
        "INSERT OR REPLACE INTO requests (id, timestamp, model, original_model,"
        " duration_ms, tokens_input, tokens_output, tokens_cache, success, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, ts, model, model, 100, ti, to, tc, 1, None),
    )


def _load_archive_script():
    """Charge scripts/archive_db.py comme module (ce n'est pas un package)."""
    path = ROOT / "scripts" / "archive_db.py"
    spec = importlib.util.spec_from_file_location("archive_db_script", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["archive_db_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def _full_request_row(
    rid="id1",
    ts="2026-09-10T10:00:00Z",
    model="glm-5.1",
    ti=11,
    to=22,
    tc=33,
) -> tuple:
    """Tuple SQL complet (32 colonnes) au format `_INSERT_REQUESTS_SQL`.

    Le writer reçoit toujours un tuple de 32 valeurs : un tuple court fait
    échouer l'INSERT (item sauté, batch vide) — piège rencontré en écrivant
    ces tests, d'où ce constructeur explicite.
    """
    return (
        rid,
        ts,
        model,
        model,
        100,
        ti,
        to,
        tc,
        1,
        None,  # id..error
        "anthropic",
        0,
        None,
        None,
        "127.0.0.1",
        None,  # protocol..account_alias
        None,
        None,
        None,
        None,
        None,  # tools..response_body
        None,
        None,
        None,
        None,
        None,  # client_user_agent..geo_blocked
        None,
        None,
        None,
        None,
        None,  # geo_direct_country..free_status
        None,  # paid_status
    )


def test_full_request_row_has_32_columns():
    """Garde-fou : le layout de test doit correspondre au schéma réel."""
    assert len(_full_request_row()) == 32
    assert odb._INSERT_REQUESTS_SQL.count("?") == 32


# ── 1. token_counters_daily ──────────────────────────────────────────


def test_init_token_counters_schema_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    odb.init_token_counters_schema(conn)
    odb.init_token_counters_schema(conn)  # 2e appel : aucun effet
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "token_counters_daily" in tables
    assert "meta" in tables


def test_bump_increments_same_day_and_model(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    odb.init_token_counters_schema(conn)
    lock = __import__("threading").Lock()
    for _ in range(3):
        odb.bump_token_counters(
            conn,
            lock,
            model="glm-5.1",
            date="2026-09-10",
            tokens_input=10,
            tokens_output=5,
            tokens_cache=1,
        )
    row = conn.execute(
        "SELECT tokens_input, tokens_output, tokens_cache, requests"
        " FROM token_counters_daily WHERE model='glm-5.1' AND date='2026-09-10'"
    ).fetchone()
    assert row == (30, 15, 3, 3), f"agrégat incorrect: {row}"


def test_bump_separates_models_and_days(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    odb.init_token_counters_schema(conn)
    lock = __import__("threading").Lock()
    odb.bump_token_counters(conn, lock, model="a", date="2026-09-10", tokens_input=1, tokens_output=0, tokens_cache=0)
    odb.bump_token_counters(conn, lock, model="b", date="2026-09-10", tokens_input=2, tokens_output=0, tokens_cache=0)
    odb.bump_token_counters(conn, lock, model="a", date="2026-09-11", tokens_input=4, tokens_output=0, tokens_cache=0)
    rows = conn.execute("SELECT model, date, tokens_input FROM token_counters_daily ORDER BY model, date").fetchall()
    assert rows == [("a", "2026-09-10", 1), ("a", "2026-09-11", 4), ("b", "2026-09-10", 2)]


def test_restore_reads_the_aggregate(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    odb.init_token_counters_schema(conn)
    lock = __import__("threading").Lock()
    odb.bump_token_counters(
        conn, lock, model="glm-5.1", date="2026-09-10", tokens_input=100, tokens_output=50, tokens_cache=7
    )
    odb.bump_token_counters(
        conn, lock, model="glm-5.1", date="2026-09-11", tokens_input=200, tokens_output=60, tokens_cache=3
    )
    out = odb.restore_token_counters(conn)
    assert out["glm-5.1"] == {"input": 300, "output": 110, "cache": 10}


def test_backfill_matches_a_direct_group_by(tmp_path):
    """Le backfill doit produire EXACTEMENT le même total qu'un GROUP BY direct."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _requests_table(conn)
    odb.init_token_counters_schema(conn)
    for i, (day, ti, to, tc) in enumerate(
        [("2026-09-01", 10, 1, 0), ("2026-09-01", 20, 2, 1), ("2026-09-02", 30, 3, 2)]
    ):
        _insert_request(conn, f"r{i}", f"{day}T10:00:00Z", ti=ti, to=to, tc=tc)
    conn.commit()

    expected = conn.execute(
        "SELECT COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0),"
        " COALESCE(SUM(tokens_cache),0) FROM requests"
    ).fetchone()

    n = odb.backfill_token_counters(conn, __import__("threading").Lock())
    assert n == 2, "2 couples (model, date) attendus"
    got = conn.execute(
        "SELECT COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0),"
        " COALESCE(SUM(tokens_cache),0) FROM token_counters_daily"
    ).fetchone()
    assert got == expected, f"backfill {got} != GROUP BY {expected}"


def test_backfill_is_single_shot(tmp_path):
    """Le curseur empêche de refaire le gros GROUP BY à chaque démarrage."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _requests_table(conn)
    odb.init_token_counters_schema(conn)
    _insert_request(conn, "r0", "2026-09-01T10:00:00Z")
    conn.commit()
    lock = __import__("threading").Lock()

    assert odb.backfill_token_counters(conn, lock) > 0
    assert odb.get_meta(conn, "token_backfill_done") == odb.DAY_BUCKET_VERSION
    assert odb.backfill_token_counters(conn, lock) == 0, "2e passage doit être un no-op"


def test_bump_is_fail_soft_on_broken_connection():
    """Une erreur de compteur ne doit JAMAIS remonter (l'insert prime)."""
    conn = sqlite3.connect(":memory:")  # pas de table token_counters_daily
    lock = __import__("threading").Lock()
    odb.bump_token_counters(conn, lock, model="x", date="2026-09-10", tokens_input=1, tokens_output=1, tokens_cache=1)


def test_execute_batch_sync_calls_counter_fn(tmp_path):
    """Le writer incrémente l'agrégat pour chaque ligne `requests` insérée."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _requests_table(conn)
    odb.init_token_counters_schema(conn)
    seen: list[tuple] = []
    # Timestamp en position 1, model 2, tokens 5/6/7 (layout _INSERT_REQUESTS_SQL).
    row = _full_request_row(ti=11, to=22, tc=33)
    n = odb.execute_batch_sync(
        conn,
        __import__("threading").Lock(),
        [row],
        lambda r: r,
        debug_fn=lambda *_a, **_k: None,
        counter_fn=lambda *a: seen.append(a),
    )
    assert n == 1, "la ligne doit être insérée (sinon le test ne prouve rien)"
    assert seen == [("glm-5.1", "2026-09-10T10:00:00Z", 11, 22, 33)], seen


def test_counter_fn_error_does_not_lose_the_insert(tmp_path):
    """Un counter_fn qui explose ne doit pas faire sauter la ligne."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _requests_table(conn)
    row = _full_request_row()

    def _boom(*_a):
        raise RuntimeError("compteur cassé")

    n = odb.execute_batch_sync(
        conn,
        __import__("threading").Lock(),
        [row],
        lambda r: r,
        debug_fn=lambda *_a, **_k: None,
        counter_fn=_boom,
    )
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_writer_aggregate_matches_inserted_rows(tmp_path):
    """Bout en bout : un batch inséré produit l'agrégat attendu."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _requests_table(conn)
    odb.init_token_counters_schema(conn)
    lock = __import__("threading").Lock()
    batch = [_full_request_row(rid=f"r{i}", ts=f"2026-09-1{i}T10:00:00Z", ti=10, to=1, tc=0) for i in range(3)]
    n = odb.execute_batch_sync(
        conn,
        lock,
        batch,
        lambda r: r,
        debug_fn=lambda *_a, **_k: None,
        # No-op lock : execute_batch_sync tient DÉJÀ `lock`, et threading.Lock
        # n'est pas réentrant → réutiliser le même lock ici se bloquerait
        # indéfiniment (c'est exactement le rôle de _null_lock côté opencode).
        counter_fn=lambda m, ts, ti, to, tc: odb.bump_token_counters(
            conn,
            __import__("contextlib").nullcontext(),
            model=m,
            date=str(ts)[:10],
            tokens_input=ti,
            tokens_output=to,
            tokens_cache=tc,
        ),
    )
    assert n == 3
    totals = odb.restore_token_counters(conn)
    assert totals["glm-5.1"] == {"input": 30, "output": 3, "cache": 0}


def test_restore_token_counters_uses_aggregate_not_group_by():
    """Contrat statique : plus de GROUP BY sur `requests` dans le restore."""
    import inspect

    import opencode as oc

    src = inspect.getsource(oc._restore_token_counters)
    assert "restore_token_counters" in src, "doit lire l'agrégat quotidien"
    assert "FROM requests GROUP BY" not in src, "le GROUP BY sur requests est banni (Phase 8)"


# ── 2. Archivage ─────────────────────────────────────────────────────


@pytest.fixture
def archive_env(tmp_path):
    """Base live + arborescence logs, comme en production."""
    logs = tmp_path / "logs"
    logs.mkdir()
    db_path = logs / "requests.db"
    conn = sqlite3.connect(db_path)
    _requests_table(conn)
    now = dt.datetime.now(dt.UTC)
    for days_ago in (200, 150, 120, 100, 61, 40, 10, 1):
        ts = (now - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_request(conn, f"r{days_ago}", ts)
    conn.commit()
    conn.close()
    return db_path


def test_build_plan_counts_old_and_recent(archive_env):
    mod = _load_archive_script()
    plan = mod.build_plan(archive_env, 90)
    assert plan["total_rows"] == 8
    assert plan["old_rows"] == 4, "4 lignes > 90 jours"
    assert plan["recent_rows"] == 4
    assert len(plan["months"]) == 4


def test_archive_dry_run_writes_nothing(archive_env):
    mod = _load_archive_script()
    before = archive_env.stat().st_size
    rc = mod.archive(archive_env, 90, apply=False, force=True)
    assert rc == 0
    assert archive_env.stat().st_size == before, "dry-run ne doit rien modifier"
    assert not (archive_env.parent / mod.ARCHIVE_DIRNAME).exists()


def test_archive_moves_rows_without_data_loss(archive_env):
    mod = _load_archive_script()
    rc = mod.archive(archive_env, 90, apply=True, force=True)
    assert rc == 0

    live = sqlite3.connect(f"file:{archive_env}?mode=ro", uri=True)
    n_live = live.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    live.close()

    archives = sorted((archive_env.parent / mod.ARCHIVE_DIRNAME).glob("*.db"))
    assert len(archives) == 4, f"4 archives mensuelles attendues, vu {archives}"
    n_arch = 0
    for f in archives:
        c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        n_arch += c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        c.close()

    assert n_live == 4
    assert n_arch == 4
    assert n_live + n_arch == 8, "AUCUNE ligne ne doit disparaître"


def test_archive_preserves_token_totals(archive_env):
    """Le total de tokens (live + archives) doit être identique à l'original."""
    mod = _load_archive_script()

    def _total(path):
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        v = c.execute("SELECT COALESCE(SUM(tokens_input),0) FROM requests").fetchone()[0]
        c.close()
        return v

    before = _total(archive_env)
    mod.archive(archive_env, 90, apply=True, force=True)
    after = _total(archive_env) + sum(_total(f) for f in (archive_env.parent / mod.ARCHIVE_DIRNAME).glob("*.db"))
    assert after == before, f"tokens perdus: {before} → {after}"


def test_archive_is_idempotent(archive_env):
    mod = _load_archive_script()
    assert mod.archive(archive_env, 90, apply=True, force=True) == 0
    plan = mod.build_plan(archive_env, 90)
    assert plan["old_rows"] == 0
    assert mod.archive(archive_env, 90, apply=True, force=True) == 0


def test_archive_refuses_when_wal_is_active(archive_env):
    """Un WAL non vide = une instance écrit → refus sans --force."""
    wal = Path(str(archive_env) + "-wal")
    wal.write_bytes(b"x" * 100)
    mod = _load_archive_script()
    rc = mod.archive(archive_env, 90, apply=True, force=False)
    assert rc == 1, "doit refuser"
    # La base est intacte.
    c = sqlite3.connect(f"file:{archive_env}?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 8
    c.close()


def test_archive_keeps_a_verified_backup(archive_env):
    mod = _load_archive_script()
    mod.archive(archive_env, 90, apply=True, force=True)
    backups = list(archive_env.parent.glob("requests.bak-*.db"))
    assert len(backups) == 1, "une copie de sécurité doit être conservée"
    c = sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 8, "backup = état initial"
    c.close()


def test_archive_no_rows_to_move(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    db_path = logs / "requests.db"
    conn = sqlite3.connect(db_path)
    _requests_table(conn)
    _insert_request(conn, "recent", "2099-01-01T00:00:00Z")
    conn.commit()
    conn.close()
    mod = _load_archive_script()
    assert mod.archive(db_path, 90, apply=True, force=True) == 0


def test_archive_creates_timestamp_index(archive_env):
    """Les archives sont interrogées par date : l'index doit exister."""
    mod = _load_archive_script()
    mod.archive(archive_env, 90, apply=True, force=True)
    for f in (archive_env.parent / mod.ARCHIVE_DIRNAME).glob("*.db"):
        c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        c.close()
        assert "idx_archive_timestamp" in idx, f"index manquant dans {f.name}"
