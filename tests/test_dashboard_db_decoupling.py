"""test_dashboard_db_decoupling.py — [P2 perf] découplage dashboard/DB.

Couvre :
  * P2.1 : les lectures dashboard passent par une connexion READ-ONLY
    dédiée (WAL : lecteurs concurrents du writer) et ne tiennent PLUS le
    lock du writer — une agrégation lente n'étouffe plus les inserts proxy.
    Test de non-régression « inserts non bloqués pendant agrégation ».
  * Fallback sûr : sans chemin RO (tests :memory:) la lecture retombe sur
    la connexion partagée SOUS lock (résultat identique).
  * P2.3 : cache /api/history/filters invalidé par DELETE /api/history +
    fusion des tools fournis par le writer (tools_provider).
"""

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI

import dashboard.api as api
from dashboard.api import register_dashboard


@pytest.fixture
def wal_db(tmp_path):
    """DB fichier en WAL avec schéma minimal requests."""
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests ("
        " id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL,"
        " original_model TEXT, duration_ms INTEGER, tokens_input INTEGER,"
        " tokens_output INTEGER, tokens_cache INTEGER, success INTEGER,"
        " account_alias TEXT, tools_used TEXT)"
    )
    conn.commit()
    yield conn, str(db_path)
    conn.close()


@pytest.fixture
def app_ro(wal_db, tmp_path):
    conn, db_path = wal_db
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    register_dashboard(fast, str(tmp_path), conn)
    return fast


def _reset_globals():
    """Neutralise l'état module entre tests (thread-locals pointent ailleurs)."""
    api._ro_db_path = ""
    api._filters_cache = None
    api._tools_provider = None
    api._shared_conn = None


@pytest.mark.asyncio
async def test_read_only_connection_is_readonly(wal_db, tmp_path):
    """La connexion RO dédiée lit et refuse toute écriture (mode=ro)."""
    conn, db_path = wal_db
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    try:
        register_dashboard(fast, str(tmp_path), conn)
        assert api._ro_db_path == db_path

        def probe():
            ro = api._get_ro_conn()
            assert ro is not None, "connexion RO indisponible"
            n = ro.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
            try:
                ro.execute(
                    "INSERT INTO requests(id, timestamp, model) VALUES ('x', 't', 'm')"
                )
                wrote = True
            except sqlite3.Error:
                wrote = False
            return n, wrote

        # NB : la connexion est thread-local — le test s'exécute dans LE MÊME
        # thread worker que les lectures dashboard réelles.
        n, wrote = await asyncio.to_thread(probe)
        assert n == 0
        assert not wrote, "la connexion RO ne doit jamais accepter une écriture"
    finally:
        _reset_globals()


@pytest.mark.asyncio
async def test_inserts_not_blocked_by_slow_dashboard_read(wal_db, tmp_path):
    """Non-régression P2.1 : une lecture dashboard LENTE ne bloque plus les
    inserts du proxy (elle ne tient plus le lock writer)."""
    conn, db_path = wal_db
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    try:
        register_dashboard(fast, str(tmp_path), conn)

        started = threading_started = None

        def slow_read(_db):
            # agrégation simulée : 400 ms SUR la connexion RO, en bloquant le
            # thread worker (comme un vrai scan SQL) mais SANS tenir le lock.
            time.sleep(0.4)
            return _db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

        read_task = asyncio.create_task(api._db_read_sync(slow_read))
        await asyncio.sleep(0.05)  # la lecture est maintenant en vol

        def do_insert():
            conn.execute(
                "INSERT INTO requests(id, timestamp, model) VALUES ('i1', '2026-08-26T00:00:00Z', 'glm')"
            )
            conn.commit()

        t0 = time.monotonic()
        await asyncio.to_thread(do_insert)
        insert_ms = (time.monotonic() - t0) * 1000

        count = await read_task
        assert count == 1
        # L'insert n'a PAS attendu la fin de la lecture (400 ms) : marge large
        # pour la CI Windows lente.
        assert insert_ms < 250, f"insert bloqué {insert_ms:.0f}ms par la lecture dashboard"
        del started, threading_started
    finally:
        _reset_globals()


@pytest.mark.asyncio
async def test_fallback_shared_conn_when_no_ro(tmp_path, monkeypatch):
    """Sans chemin RO (:memory:), la lecture retombe sous lock partagé."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE requests (id TEXT, model TEXT)")
    conn.commit()
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    try:
        register_dashboard(fast, str(tmp_path), conn)
        monkeypatch.setattr(api, "_ro_db_path", "")

        def q(db):
            return db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

        assert await api._db_read_sync(q) == 0
    finally:
        _reset_globals()


@pytest.mark.asyncio
async def test_filters_cache_and_tools_provider(app_ro):
    """P2.3 : filters cachés ; DELETE invalide ; tools_provider fusionné."""
    calls = {"n": 0}
    real = api._db_read_sync

    async def counting(fn):
        if getattr(fn, "__name__", "") == "_query_filters":
            calls["n"] += 1
        return await real(fn)

    from starlette.testclient import TestClient

    try:
        import dashboard.api as a

        monkey_target = a._db_read_sync
        a._db_read_sync = counting
        api._tools_provider = lambda: {"tool_from_writer"}
        with TestClient(app_ro) as client:
            r1 = client.get("/api/history/filters").json()
            r2 = client.get("/api/history/filters").json()
        assert r1 == r2
        assert "tool_from_writer" in r1["tools_used"]
        assert calls["n"] == 1, "le scan SQL doit être servi du cache au 2ᵉ appel"

        # Invalidation via DELETE /api/history
        with TestClient(app_ro) as client:
            client.request("DELETE", "/api/history?all=true")
        assert a._filters_cache is None
    finally:
        a._db_read_sync = monkey_target
        _reset_globals()
