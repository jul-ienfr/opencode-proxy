"""
app.db — SQLite WAL requests.db : schéma, batch writer, maintenance.

[P5 tranche 1] Extraction de la LOGIQUE DB depuis opencode.py (audit-perf-
qualité Phase 5, PR isolée n°1). Ce module est PUR : aucun import du projet
(opencode/config/dashboard interdits) — tout ce qui varie est injecté :

  * ``conn`` / ``lock`` / état de batch passés en paramètre (l'état vivant
    reste la propriété d'opencode.py, qui délègue via des wrappers d'un
    ligne — les seams de test ``oc._conn`` / ``oc._db_queue`` /
    ``monkeypatch.setattr`` continuent de fonctionner car les wrappers
    lisent les globales d'opencode À L'APPEL) ;
  * ``redact_fn`` injecté dans materialize_db_row (_redact reste chez
    opencode) ;
  * ``debug_fn`` / ``log_fn`` injectés pour la journalisation.

Contrats couverts par tests/test_db_offload.py :
  - _materialize_db_row produit EXACTEMENT le même tuple SQL 32 colonnes ;
  - la queue transporte des lignes BRUTES (_DbRowRaw) ;
  - les corps > 2 Mo sont stubés côté caller (jamais épinglés en queue).
"""

import json
import sqlite3
import time

# ── Constantes ──────────────────────────────────────────────────────

MAX_BODY_STORAGE = 100_000  # Max chars stored per request/response body in DB
DB_RAW_SIZE_CAP = 2_000_000  # [D1] au-delà : résumé compact mis en queue


# ── Corps : tronquage / estimation / stub ───────────────────────────


def truncate_body_for_storage(
    body: dict | None, max_chars: int = MAX_BODY_STORAGE
) -> str | None:
    """Serialize body to JSON, truncating messages array if needed to stay under max_chars.

    Keeps model, tools, and a summary of messages to preserve context while
    avoiding the memory waste of serializing a 10MB body just to keep 100K.

    Optimized: builds truncated version first, only falls back to full
    serialization if the truncated version is small enough.
    """
    if not body:
        return None
    # Quick size estimate: sum of string lengths of non-messages fields
    # This avoids full json.dumps for large bodies
    estimate = sum(len(str(v)) for k, v in body.items() if k != "messages")
    messages = body.get("messages", [])
    if messages:
        # Estimate first 2 messages + truncation marker
        for msg in messages[:2]:
            estimate += len(str(msg))
        estimate += 80  # truncation marker overhead

    if estimate <= max_chars:
        # Likely fits — do full serialization (single pass)
        full = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(full) <= max_chars:
            return full
        # Fell through: full was too big, build truncated version
    # Build truncated version (skip full serialization of large body)
    truncated = {k: v for k, v in body.items() if k != "messages"}
    if messages:
        truncated["messages"] = messages[:2] + [
            {"_truncated": True, "original_count": len(messages)}
        ]
    result = json.dumps(truncated, ensure_ascii=False, separators=(",", ":"))
    if len(result) > max_chars:
        result = result[:max_chars]
    return result


def quick_body_size(body) -> int:
    """[D1 perf] Estimation O(texte) SANS sérialisation ni repr — somme des
    longueurs des chaînes directement accessibles (champs top-level +
    contenus de messages). Suffisante pour décider si un corps est trop
    volumineux pour la queue ; le tronquage exact reste dans le writer.

    [P5.5 perf] early-exit dès DB_RAW_SIZE_CAP atteint — inutile de scanner
    10 Mo de messages quand on sait déjà qu'on va stubber."""
    if not isinstance(body, dict):
        return 0
    total = 0
    for v in body.values():
        if type(v) is str:
            total += len(v)
            if total > DB_RAW_SIZE_CAP:
                return total
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if type(c) is str:
                total += len(c)
                if total > DB_RAW_SIZE_CAP:
                    return total
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict):
                        t = p.get("text") or p.get("thinking") or ""
                        if type(t) is str:
                            total += len(t)
                            if total > DB_RAW_SIZE_CAP:
                                return total
                        if p.get("input") is not None and not isinstance(
                            p.get("input"), (str, int, float, bool)
                        ):
                            total += 256  # tool_use input : borne grossière
                            if total > DB_RAW_SIZE_CAP:
                                return total
    return total


def compact_body_stub(body) -> dict:
    """[D1] Corps > DB_RAW_SIZE_CAP → résumé compact (le writer appliquera
    truncate+redact comme à tout autre corps). Évite d'épingler 10 Mo dans
    la queue sous stall DB."""
    stub: dict = {"_oversize": True}
    if isinstance(body, dict):
        for k in ("model", "stream", "max_tokens"):
            if k in body:
                stub[k] = body[k]
        sysv = body.get("system")
        if isinstance(sysv, str):
            stub["system_head"] = sysv[:500]
        elif isinstance(sysv, list) and sysv and isinstance(sysv[0], dict):
            stub["system_head"] = str(sysv[0].get("text", ""))[:500]
        msgs = body.get("messages")
        if isinstance(msgs, list):
            stub["_message_count"] = len(msgs)
            first = msgs[0] if msgs else None
            if isinstance(first, dict):
                c = first.get("content")
                stub["first_message_role"] = first.get("role")
                stub["first_message_head"] = (
                    c[:300] if isinstance(c, str) else str(c)[:300]
                )
    return stub


# ── Ligne brute ─────────────────────────────────────────────────────


class DbRowRaw:
    """[D1 perf] Ligne DB brute : dumps tools + tronquage + redaction
    s'exécutent dans le THREAD WRITER (materialize_db_row), plus dans la
    coroutine appelante — 0,5-3 ms/requête rendus à l'event loop.
    L'ordre d'écriture est préservé (même queue unique)."""

    __slots__ = ("head", "tail", "request_body", "response_body", "tools", "tools_used")

    def __init__(self, head: tuple, tail: tuple, *, request_body, response_body, tools, tools_used):
        # head = champs SQL avant tools_json (id..account_alias),
        # tail = champs après response_body_json (client_user_agent..station).
        self.head = head
        self.tail = tail
        self.request_body = request_body
        self.response_body = response_body
        self.tools = tools
        self.tools_used = tools_used


def materialize_db_row(raw: DbRowRaw, *, redact_fn, tools_seen: set | None = None) -> tuple:
    """Thread writer : sérialise/tronque/redige une ligne brute (CPU hors loop).

    ``redact_fn`` injecté (la redaction reste la propriété d'opencode) ;
    ``tools_seen`` : set optionnel alimenté pour /api/history/filters.
    """
    tools_json = json.dumps(raw.tools) if raw.tools else "[]"
    tools_used_json = json.dumps(list(dict.fromkeys(raw.tools_used))) if raw.tools_used else "[]"
    # [P2 perf] registre des tools utilisés pour /api/history/filters —
    # tenu à jour ici côté writer (thread unique) : le dashboard n'a plus à
    # scanner TOUTE la table JSON à chaque requête de filtres.
    if raw.tools_used and tools_seen is not None:
        tools_seen.update(raw.tools_used)
    request_body_json = (
        redact_fn(truncate_body_for_storage(raw.request_body)) if raw.request_body else None
    )
    response_body_json = (
        redact_fn(truncate_body_for_storage(raw.response_body)) if raw.response_body else None
    )
    return raw.head + (tools_json, tools_used_json, request_body_json, response_body_json) + raw.tail


# ── Timestamps ──────────────────────────────────────────────────────


def normalize_timestamp_utc(timestamp: str) -> str:
    """Naive local wall time → UTC+Z ; les valeurs déjà en Z passent inchangées."""
    import datetime as _dt

    if timestamp.endswith("Z"):
        return timestamp
    try:
        return (
            _dt.datetime.fromisoformat(timestamp)
            .astimezone()
            .astimezone(_dt.UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except ValueError:
        return timestamp  # unparseable — store as-is rather than dropping the row


# ── Schéma ──────────────────────────────────────────────────────────

_SCHEMA_REQUESTS = """
    CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        model TEXT NOT NULL,
        original_model TEXT,
        duration_ms INTEGER,
        tokens_input INTEGER,
        tokens_output INTEGER,
        tokens_cache INTEGER,
        success INTEGER,
        error TEXT,
        protocol TEXT,
        is_stream INTEGER,
        thinking TEXT,
        effort TEXT
    )
"""

_REQUESTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_model ON requests(model)",
    "CREATE INDEX IF NOT EXISTS idx_success ON requests(success)",
    "CREATE INDEX IF NOT EXISTS idx_account ON requests(account_alias)",
    "CREATE INDEX IF NOT EXISTS idx_ts_model ON requests(timestamp, model)",
    "CREATE INDEX IF NOT EXISTS idx_free_ip ON requests(free_model_ip)",
    # [P2 perf] filtre historique par modèle original (dropdown dashboard)
    "CREATE INDEX IF NOT EXISTS idx_original_model ON requests(original_model)",
]

_REQUEST_COLUMN_MIGRATIONS = [
    ("protocol", "NULL"),
    ("is_stream", "0"),
    ("thinking", "NULL"),
    ("effort", "NULL"),
    ("client_ip", "NULL"),
    ("account_alias", "NULL"),
    ("tools", "NULL"),
    ("tools_used", "NULL"),
    ("request_body", "NULL"),
    ("response_body", "NULL"),
    ("client_user_agent", "NULL"),
    ("free_model_ip", "NULL"),
    ("identity", "NULL"),
    ("geo_country", "NULL"),
    ("geo_blocked", "0"),
    ("hedged", "0"),
    ("winner_station", "NULL"),
    ("geo_direct_country", "NULL"),
    ("geo_direct_ip", "NULL"),
    ("geo_via_vpn", "0"),
    ("geo_allowed", "NULL"),
    # [Étape 2 — O2] jambes fallback corrélées par req_id (remplies par
    # _save_request via peek _FALLBACK_CTX + statut paid ; NULL = pas de fallback)
    ("free_status", "NULL"),
    ("paid_status", "NULL"),
]

_INSERT_REQUESTS_SQL = """
    INSERT OR REPLACE INTO requests (id, timestamp, model, original_model, duration_ms,
        tokens_input, tokens_output, tokens_cache, success, error,
        protocol, is_stream, thinking, effort, client_ip, account_alias, tools, tools_used,
        request_body, response_body, client_user_agent, free_model_ip, identity, geo_country, geo_blocked,
        geo_direct_country, geo_direct_ip, geo_via_vpn, geo_allowed, station, free_status, paid_status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# [P1.2] INSERT free_model_usage préparé par l'appelant (timestamp inclus) —
# consommé par le writer batché ; le masquage de clé reste côté caller.
_INSERT_FREE_USAGE_SQL = (
    "INSERT INTO free_model_usage "
    "(timestamp, paid_model, free_model, api_key, workspace_id, status, "
    " tokens_input, tokens_output, duration_ms, ip) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def init_requests_schema(
    conn: sqlite3.Connection, *, busy_timeout: int, cache_size: int, mmap_size: int
) -> int:
    """PRAGMAs WAL/NORMAL + schéma requests + migrations colonnes + index.

    Retourne le nombre de rows à timestamps naïfs ([30] canary — l'appelant
    avertit l'opérateur que scripts/migrate_timestamps_utc.py est à lancer).
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL+NORMAL: safe crash-resilient
    conn.execute(f"PRAGMA cache_size=-{cache_size}")  # page cache
    conn.execute("PRAGMA temp_store=MEMORY")  # temp tables in RAM
    conn.execute(f"PRAGMA mmap_size={mmap_size}")  # memory-mapped I/O
    conn.execute(_SCHEMA_REQUESTS)
    for col, default in _REQUEST_COLUMN_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass
    for stmt in _REQUESTS_INDEXES:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    # [plan v10 §4 Lot 4] colonne station INTEGER (filtres ?station=) +
    # index composé station+timestamp (budget §7).
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN station INTEGER DEFAULT NULL")
    except Exception:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_station_ts ON requests(station, timestamp)"
    )
    conn.commit()
    # [30] Canary: mixed naive/UTC timestamps break ORDER BY timestamp DESC
    try:
        naive = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE timestamp NOT LIKE '%Z'"
        ).fetchone()[0]
    except Exception:
        naive = 0
    return naive


def init_free_usage_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS free_model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            paid_model TEXT NOT NULL,
            free_model TEXT NOT NULL,
            api_key TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            status INTEGER NOT NULL,
            tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            ip TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_free_ts ON free_model_usage(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_free_model ON free_model_usage(free_model)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_free_key ON free_model_usage(api_key)")
    try:
        conn.execute("ALTER TABLE free_model_usage ADD COLUMN ip TEXT DEFAULT ''")
    except Exception:
        pass  # Column already exists
    conn.commit()


# ── Batch state ─────────────────────────────────────────────────────


class BatchState:
    """Compteurs de commit groupé (propriété de l'app hôte, mutables ici)."""

    __slots__ = ("pending", "last_commit", "commit_interval", "commit_batch")

    def __init__(self, *, commit_interval: float, commit_batch: int):
        self.pending = 0
        self.last_commit = time.monotonic()
        self.commit_interval = float(commit_interval)  # s entre commits périodiques
        self.commit_batch = int(commit_batch)  # force commit après N inserts


# ── Inserts / flush / batch ─────────────────────────────────────────


def flush(conn: sqlite3.Connection, lock, state: BatchState, *, debug_fn) -> None:
    """Force a pending commit. Called periodically and before shutdown."""
    with lock:
        if state.pending > 0:
            try:
                conn.commit()
                debug_fn(f"  [db] _db_flush: committed {state.pending} pending inserts")
            except Exception as e:
                debug_fn(f"  [db] _db_flush commit FAILED: {type(e).__name__}: {e}")
                # Try rollback to recover the connection for future operations
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Always reset counter to avoid stuck state — even if commit failed,
            # uncommitted rows will be lost but new inserts can proceed normally
            state.pending = 0
            state.last_commit = time.monotonic()


def insert_sync(
    conn: sqlite3.Connection,
    lock,
    state: BatchState,
    row: tuple,
    *,
    debug_fn,
) -> None:
    """Synchronous DB insert d'un tuple SQL complet (32 colonnes) — appelé
    via thread pool.

    Batches commits: accumulates INSERTs and commits every commit_batch
    inserts or every commit_interval seconds, whichever comes first.
    Reduces fsync overhead under load (50 req/s → ~1 commit/s instead of 50).
    """
    t0 = time.monotonic()
    # Lock the entire execute+commit block to prevent InterfaceError when
    # flush or wal_checkpoint runs concurrently on another thread.
    with lock:
        conn.execute(_INSERT_REQUESTS_SQL, row)
        # Batch commit logic
        state.pending += 1
        now = time.monotonic()
        elapsed = now - state.last_commit
        if state.pending >= state.commit_batch or elapsed >= state.commit_interval:
            try:
                conn.commit()
                debug_fn(
                    f"  [db] _db_insert_sync: batch-committed {state.pending} inserts ({elapsed:.1f}s) in {(time.monotonic() - t0) * 1000:.1f}ms"
                )
            except Exception as e:
                debug_fn(f"  [db] _db_insert_sync commit FAILED: {type(e).__name__}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Always reset counter to avoid stuck state
            state.pending = 0
            state.last_commit = now
        else:
            debug_fn(
                f"  [db] _db_insert_sync: queued req_id={row[0]} (pending={state.pending}, {elapsed:.1f}s since last commit)"
            )


def execute_batch_sync(
    conn: sqlite3.Connection,
    lock,
    batch: list,
    materialize_fn,
    *,
    debug_fn,
) -> int:
    """Execute a batch of DB inserts in a single transaction (called in thread pool).

    ``materialize_fn(item)`` transforme une _DbRowRaw brute en tuple SQL
    (sérialisation/tronquage/redaction ICI, thread writer — [D1]).

    [P1.2 perf] Les items peuvent être des tuples taggés
    ``(table, payload)`` avec ``table`` ∈ {"requests", "free_usage"} :
    le writer matérialise et INSERT dans la table cible ICI (thread),
    commits groupés inchangés. Sémantique fail-soft : une erreur SQL sur
    un item est loguée et l'item sauté — jamais propagée à la requête.
    Les items nus (_DbRowRaw / tuple SQL) restent acceptés (= requests).
    """
    if not batch:
        return 0
    inserted = 0
    with lock:
        for item in batch:
            try:
                if isinstance(item, tuple) and item and isinstance(item[0], str):
                    table, payload = item[0], item[1]
                else:
                    table, payload = "requests", item
                if table == "free_usage":
                    conn.execute(_INSERT_FREE_USAGE_SQL, payload)
                    inserted += 1
                    continue
                row = materialize_fn(payload) if isinstance(payload, DbRowRaw) else payload
                conn.execute(_INSERT_REQUESTS_SQL, row)
                inserted += 1
            except Exception as e:
                debug_fn(f"  [db] batch item skipped ({type(e).__name__}: {e})")
        try:
            conn.commit()
        except Exception as e:
            debug_fn(f"  [db] batch commit FAILED: {type(e).__name__}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return 0
        return inserted


# ── Maintenance ─────────────────────────────────────────────────────


def vacuum_if_needed(conn: sqlite3.Connection, lock, deleted_rows: int) -> None:
    """Reclaim disk space after cleanup deletes (Vague 4/(g)).

    SQLite keeps freed pages inside the file until VACUUM — without it the
    DB only grows. Runs at most daily (cleanup cadence), only when rows
    were actually deleted. VACUUM needs exclusive access, so it holds the
    same lock as inserts/checkpoints; it commits any pending transaction
    first and is itself transactional (safe on failure).
    """
    if deleted_rows <= 0:
        return
    with lock:
        try:
            # A batched-insert transaction may still be open; VACUUM refuses
            # to run inside one. Committing it early is harmless — the rows
            # were destined to commit within the batch window anyway.
            if conn.in_transaction:
                conn.commit()
            conn.execute("VACUUM")
        except Exception:
            pass  # l'app hôte journalise via son wrapper


def cleanup_old_bodies(
    conn: sqlite3.Connection,
    lock,
    retention_days: int = 7,
    delete_after_days: int = 30,
    *,
    log_fn,
    debug_fn,
) -> int:
    """Clean up old request data to prevent DB bloat.

    Two-phase cleanup:
    1. DELETE entire rows older than delete_after_days (30d default) — full removal
    2. NULLIFY bodies for rows between retention_days and delete_after_days — keep metadata

    Bodies account for ~95% of DB storage. This keeps recent bodies for debugging
    while preventing unbounded growth. Called periodically by background task.
    """
    try:
        # Phase 1: Delete old rows entirely
        cutoff_delete = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delete_after_days * 86400)
        )
        cursor = conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff_delete,))
        deleted = cursor.rowcount
        cursor2 = conn.execute(
            "DELETE FROM free_model_usage WHERE timestamp < ?", (cutoff_delete,)
        )
        deleted2 = cursor2.rowcount

        # Phase 2: Nullify bodies for 7-30 day old rows
        cutoff_null = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400)
        )
        cursor3 = conn.execute(
            "UPDATE requests SET request_body = NULL, response_body = NULL "
            "WHERE timestamp < ? AND (request_body IS NOT NULL OR response_body IS NOT NULL)",
            (cutoff_null,),
        )
        cleaned = cursor3.rowcount

        total = deleted + deleted2 + cleaned
        if total > 0:
            conn.commit()
            debug_fn(
                f"  [db] cleanup: deleted {deleted}+{deleted2} old rows, cleared bodies from {cleaned} requests"
            )
            log_fn(
                f"  DB CLEANUP: deleted {deleted + deleted2} old rows, cleared {cleaned} bodies (>{retention_days}d)"
            )
            vacuum_if_needed(conn, lock, deleted + deleted2)
        return deleted + deleted2 + cleaned
    except Exception as e:
        debug_fn(f"  [db] cleanup error: {type(e).__name__}: {e}")
        return 0


def wal_checkpoint(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def log_free_usage(
    conn: sqlite3.Connection,
    lock,
    *,
    paid_model: str,
    free_model: str,
    api_key_masked: str,
    workspace_id: str,
    status: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_ms: int = 0,
    ip: str = "",
) -> None:
    """[P5 tranche 4] INSERT free_model_usage sous le lock writer (commit
    immédiat : la table alimente le dashboard quotas, pas besoin de batch).

    ``api_key_masked`` est DÉJÀ masqué par l'appelant (jamais la clé pleine).
    Lève sur erreur SQL — l'app hôte journalise et continue (fail-soft)."""
    import datetime as _dt

    timestamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with lock:
        conn.execute(
            _INSERT_FREE_USAGE_SQL,
            (
                timestamp,
                paid_model,
                free_model,
                api_key_masked,
                workspace_id,
                status,
                tokens_in,
                tokens_out,
                duration_ms,
                ip,
            ),
        )
        conn.commit()


# [plan 30/08 Lot B1] purge des LIGNES > 90 jours au passage de la
# maintenance hebdo (dimanche 03:00) — avant le VACUUM pour rendre les
# pages libérées. La purge corps (> body_retention_days, défaut 7 j scinde
# les corps hors taille) reste quotidienne dans cleanup_old_bodies ; cette
# purge vise la croissance structurelle du fichier (incident 30/08 : ~1 Go).
WEEKLY_PURGE_DAYS = 90

_PURGE_OLD_ROWS_SQL = "DELETE FROM requests WHERE timestamp < ?"
_PURGE_OLD_USAGE_SQL = "DELETE FROM free_model_usage WHERE timestamp < ?"


def _purge_old_rows_locked(conn: sqlite3.Connection, days: int = WEEKLY_PURGE_DAYS) -> int:
    """DELETE ≤ bornes des deux tables ; suppose le lock déjà détenu.

    Les timestamps sont TEXT ISO8601 UTC — un cutoff texte suffit ('YYYY-MM-…'
    trie proprement), pas de strftime SQLite (testable 100 % pur).

    ``days <= 0`` = no-op défensif (le garde-fou officiel reste dans
    weekly_maintain, mais l'helper ne doit jamais purger « sans borne »)."""
    if days is None or days <= 0:
        return 0
    import datetime as _dt

    cutoff = (
        (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    total = 0
    for sql in (_PURGE_OLD_ROWS_SQL, _PURGE_OLD_USAGE_SQL):
        try:
            cur = conn.execute(sql, (cutoff,))
            total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error:
            # free_model_usage peut ne pas exister sur une DB legacy —
            # la purge reste tolérante (le VACUUM tourne quand même).
            continue
    return total


def weekly_maintain(conn: sqlite3.Connection, lock, purge_days: int = WEEKLY_PURGE_DAYS) -> float:
    """Checkpoint TRUNCATE + purge > ``purge_days`` jours + VACUUM sous le
    lock writer (maintenance hebdo).

    ``purge_days`` peut être désactivé via 0 (ou None) — la maintenance
    redevient alors le checkpoint+VACUUM historique.

    Retourne la taille DB en Mo (pour le log de l'app hôte)."""
    with lock:
        if purge_days and purge_days > 0:
            _purge_old_rows_locked(conn, int(purge_days))
            # Commit explicite AVANT VACUUM : la purge ouvre une transaction
            # implicite (sqlite3 isolation_level default), et VACUUM refuse
            # de tourner dans une transaction.
            conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    import os as _os

    path = None
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                path = row[2]
                break
    except Exception:
        pass
    return round(_os.path.getsize(path) / 1024 / 1024, 1) if path else 0.0


__all__ = [
    "DB_RAW_SIZE_CAP",
    "MAX_BODY_STORAGE",
    "BatchState",
    "DbRowRaw",
    "cleanup_old_bodies",
    "compact_body_stub",
    "execute_batch_sync",
    "flush",
    "init_free_usage_schema",
    "init_requests_schema",
    "insert_sync",
    "materialize_db_row",
    "normalize_timestamp_utc",
    "quick_body_size",
    "truncate_body_for_storage",
    "vacuum_if_needed",
    "wal_checkpoint",
    "weekly_maintain",
]
