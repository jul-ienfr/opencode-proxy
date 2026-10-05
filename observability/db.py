"""
observability.db — SQLite WAL requests.db : schéma, batch writer, maintenance.

[Phase 2 refonte] Domicile CANONIQUE (déplacement pur depuis ``app/db`` —
contenu identique, seul cet en-tête change). ``app.db`` reste un shim de
re-export (façade gelée ADR-006 : ``tests/test_phase0_contracts.py`` consomme
``app.db``) jusqu'à la Phase 9 ; le nouveau code importe ``observability.db``.

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
import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

# Compteur d'anomalies DB silencieuses autrefois (migrations/index/VACUUM
# avalés par `except: pass`). Exposé pour /metrics et les tests.
db_maintenance_errors: int = 0


def _record_maintenance_error(where: str, exc: BaseException) -> None:
    """Journalise une anomalie de maintenance (jamais silencieuse)."""
    global db_maintenance_errors
    db_maintenance_errors += 1
    logger.warning("[db] maintenance %s FAILED: %s: %s", where, type(exc).__name__, exc)

# ── Constantes ──────────────────────────────────────────────────────

MAX_BODY_STORAGE = 100_000  # Max chars stored per request/response body in DB
DB_RAW_SIZE_CAP = 2_000_000  # [D1] au-delà : résumé compact mis en queue


# ── Corps : tronquage / estimation / stub ───────────────────────────


def truncate_body_for_storage(body: dict | None, max_chars: int = MAX_BODY_STORAGE) -> str | None:
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
        truncated["messages"] = messages[:2] + [{"_truncated": True, "original_count": len(messages)}]
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
                        if p.get("input") is not None and not isinstance(p.get("input"), (str, int, float, bool)):
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
                stub["first_message_head"] = c[:300] if isinstance(c, str) else str(c)[:300]
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
    request_body_json = redact_fn(truncate_body_for_storage(raw.request_body)) if raw.request_body else None
    response_body_json = redact_fn(truncate_body_for_storage(raw.response_body)) if raw.response_body else None
    return raw.head + (tools_json, tools_used_json, request_body_json, response_body_json) + raw.tail


# ── Timestamps ──────────────────────────────────────────────────────


def normalize_timestamp_utc(timestamp: str) -> str:
    """Naive local wall time → UTC+Z ; les valeurs déjà en Z passent inchangées."""
    import datetime as _dt

    if timestamp.endswith("Z"):
        return timestamp
    try:
        return _dt.datetime.fromisoformat(timestamp).astimezone().astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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

# Tables cibles acceptées sous forme de tuple taggé ``(table, payload)`` dans
# un batch — utilisé pour lever l'ambiguïté avec un tuple SQL brut (voir
# execute_batch_sync).
_TAGGED_TABLES = frozenset({"requests", "free_usage"})

# [P1.2] INSERT free_model_usage préparé par l'appelant (timestamp inclus) —
# consommé par le writer batché ; le masquage de clé reste côté caller.
_INSERT_FREE_USAGE_SQL = (
    "INSERT INTO free_model_usage "
    "(timestamp, paid_model, free_model, api_key, workspace_id, status, "
    " tokens_input, tokens_output, duration_ms, ip) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# ── [Phase 8 plan boot] Compteurs de tokens incrémentaux ─────────────
# Le restore des compteurs au boot faisait `SELECT model, SUM(tokens_*) FROM
# requests GROUP BY model` sur ~6 Go : plusieurs secondes, à chaque démarrage.
# Cette table agrège la MÊME information par (model, date) et est alimentée
# par le writer au fil de l'eau : le restore devient un `SELECT ... WHERE
# date >= ?` sur quelques centaines de lignes (<10 ms).
#
# Le curseur (`meta`) permet un premier remplissage progressif : au boot, si
# la table est vide/incomplète, on agrège l'historique en tâche de fond par
# tranches de dates, sans jamais re-scanner ce qui est déjà compté.
_SCHEMA_TOKEN_COUNTERS_DAILY = """
    CREATE TABLE IF NOT EXISTS token_counters_daily (
        model TEXT NOT NULL,
        date TEXT NOT NULL,
        tokens_input INTEGER NOT NULL DEFAULT 0,
        tokens_output INTEGER NOT NULL DEFAULT 0,
        tokens_cache INTEGER NOT NULL DEFAULT 0,
        requests INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (model, date)
    )
"""

_TOKEN_COUNTERS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tcd_date ON token_counters_daily(date)",
]

# Upsert : le writer incrémente les compteurs du jour pour le modèle.
_UPSERT_TOKEN_COUNTERS_SQL = """
    INSERT INTO token_counters_daily
        (model, date, tokens_input, tokens_output, tokens_cache, requests)
    VALUES (?, ?, ?, ?, ?, 1)
    ON CONFLICT(model, date) DO UPDATE SET
        tokens_input = tokens_input + excluded.tokens_input,
        tokens_output = tokens_output + excluded.tokens_output,
        tokens_cache = tokens_cache + excluded.tokens_cache,
        requests = requests + 1
"""

# Meta générique (curseurs d'agrégation, version de schéma backfill).
_SCHEMA_META = """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
"""

DAY_BUCKET_VERSION = "1"


def init_token_counters_schema(conn: sqlite3.Connection) -> None:
    """Crée token_counters_daily + meta (idempotent, aucun scan).

    Appelé au boot sur le chemin import → listen : uniquement des
    ``CREATE TABLE IF NOT EXISTS``, donc O(1) même sur une DB de 6 Go.
    """
    conn.execute(_SCHEMA_TOKEN_COUNTERS_DAILY)
    conn.execute(_SCHEMA_META)
    for stmt in _TOKEN_COUNTERS_INDEXES:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()


def bump_token_counters(
    conn: sqlite3.Connection,
    lock,
    *,
    model: str,
    date: str,
    tokens_input: int,
    tokens_output: int,
    tokens_cache: int,
    fail_soft: bool = True,
) -> None:
    """Incrémente les compteurs du jour (appelé par le writer, sous lock).

    Fail-soft par défaut : la comptabilité analytique ne doit JAMAIS faire
    échouer l'insertion de la requête elle-même.
    """
    try:
        with lock:
            conn.execute(
                _UPSERT_TOKEN_COUNTERS_SQL,
                (model, date, int(tokens_input or 0), int(tokens_output or 0), int(tokens_cache or 0)),
            )
    except Exception:
        if not fail_soft:
            raise


def restore_token_counters(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Compteurs cumulés par modèle, lus depuis token_counters_daily.

    Remplace le ``GROUP BY`` sur ``requests`` : quelques centaines de lignes
    au lieu de millions → <10 ms sur la DB de référence (5,9 Go).
    """
    out: dict[str, dict[str, int]] = {}
    try:
        rows = conn.execute(
            "SELECT model,"
            "       COALESCE(SUM(tokens_input), 0),"
            "       COALESCE(SUM(tokens_output), 0),"
            "       COALESCE(SUM(tokens_cache), 0)"
            " FROM token_counters_daily GROUP BY model"
        ).fetchall()
    except Exception:
        return out
    for row in rows:
        out[row[0]] = {"input": row[1], "output": row[2], "cache": row[3]}
    return out


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def set_meta(conn: sqlite3.Connection, lock, key: str, value: str) -> None:
    try:
        with lock:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
    except Exception:
        pass


def backfill_token_counters(conn: sqlite3.Connection, lock, *, chunk_days: int = 30) -> int:
    """Agrège l'historique `requests` dans token_counters_daily, par tranches.

    Un seul passage : le curseur ``token_backfill_done`` est posé à la fin.
    Conçu pour tourner en tâche de fond APRÈS le ready (jamais sur le chemin
    import → listen) — c'est le seul endroit qui refait le gros ``GROUP BY``,
    et une seule fois dans la vie de la base.

    Retourne le nombre de lignes agrégées (0 si déjà fait).
    """
    if get_meta(conn, "token_backfill_done") == DAY_BUCKET_VERSION:
        return 0
    total = 0
    try:
        with lock:
            # `substr(timestamp, 1, 10)` = YYYY-MM-DD sur les timestamps ISO
            # (les naïfs sont traités par le canary, cf. migrate_and_canary).
            rows = conn.execute(
                "SELECT model, substr(timestamp, 1, 10) AS d,"
                "       COALESCE(SUM(tokens_input), 0),"
                "       COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0),"
                "       COUNT(*)"
                " FROM requests"
                " WHERE timestamp IS NOT NULL AND length(timestamp) >= 10"
                " GROUP BY model, d"
            ).fetchall()
            for model, d, ti, to, tc, n in rows:
                conn.execute(
                    "INSERT INTO token_counters_daily"
                    " (model, date, tokens_input, tokens_output, tokens_cache, requests)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(model, date) DO UPDATE SET"
                    "   tokens_input = excluded.tokens_input,"
                    "   tokens_output = excluded.tokens_output,"
                    "   tokens_cache = excluded.tokens_cache,"
                    "   requests = excluded.requests",
                    (model, d, ti, to, tc, n),
                )
                total += 1
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('token_backfill_done', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (DAY_BUCKET_VERSION,),
            )
            conn.commit()
    except Exception:
        return 0
    return total


def init_requests_schema_fast(conn: sqlite3.Connection, *, busy_timeout: int, cache_size: int, mmap_size: int) -> None:
    """Boot rapide (Phase 2 chantier boot) : PRAGMAs WAL/NORMAL + CREATE TABLE.

    SANS migrations ALTER, SANS CREATE INDEX, SANS canary COUNT(*) — aucun
    full scan sur le chemin import → listen (DB ~6 Go : le canary seul vaut
    10-20 s). Les migrations + index + canary partent en fond post-ready via
    :func:`migrate_and_canary`.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL+NORMAL: safe crash-resilient
    conn.execute(f"PRAGMA cache_size=-{cache_size}")  # page cache
    conn.execute("PRAGMA temp_store=MEMORY")  # temp tables in RAM
    conn.execute(f"PRAGMA mmap_size={mmap_size}")  # memory-mapped I/O
    conn.execute(_SCHEMA_REQUESTS)
    conn.commit()


def migrate_and_canary(conn: sqlite3.Connection, *, report: dict | None = None) -> int:
    """Post-ready (fond) : migrations ALTER + CREATE INDEX + canary borné.

    Ordre optimisé anti full-scan : les index sur colonnes natives
    (timestamp/model/success) sont créés AVANT les migrations ALTER, pour
    fermer au plus tôt la fenêtre « requêtes dashboard sans index ». Les
    index sur colonnes migrées suivent, puis le canary.

    Canary BORNÉ (P0-4) : `SELECT 1 ... LIMIT 1001` au lieu de `COUNT(*)`
    — jamais de full scan sur DB multi-Go ; retourne le compte exact sous
    1001, 1001 si saturé (l'appelant affiche « ≥ »).

    Retourne le nombre de rows à timestamps naïfs (contrat historique :
    int). ``report`` (optionnel) reçoit le détail {added_columns,
    existing_columns, failed_ops, canary_capped}.
    L'appelant détient le lock writer (concurrence avec le writer loop).
    """
    added: list[str] = []
    existing: list[str] = []
    failed: list[str] = []
    # [P0-4] index natifs d'abord : requêtes dashboard couvertes au plus tôt.
    for stmt in _REQUESTS_INDEXES[:3]:
        try:
            conn.execute(stmt)
        except Exception as e:
            failed.append(stmt)
            _record_maintenance_error(f"index {stmt}", e)
    for col, default in _REQUEST_COLUMN_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
            added.append(col)
        except Exception as e:
            # Colonne déjà présente = cas nominal (IF NOT EXISTS indisponible
            # pour ADD COLUMN) ; toute autre erreur est journalisée + comptée.
            if "duplicate column name" not in str(e).lower():
                failed.append(f"addcol:{col}")
                _record_maintenance_error(f"addcol {col}", e)
            else:
                existing.append(col)
    for stmt in _REQUESTS_INDEXES[3:]:
        try:
            conn.execute(stmt)
        except Exception as e:
            failed.append(stmt)
            _record_maintenance_error(f"index {stmt}", e)
    # [plan v10 §4 Lot 4] colonne station INTEGER (filtres ?station=) +
    # index composé station+timestamp (budget §7).
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN station INTEGER DEFAULT NULL")
        added.append("station")
    except Exception as e:
        if "duplicate column name" not in str(e).lower():
            failed.append("addcol:station")
            _record_maintenance_error("addcol station", e)
        else:
            existing.append("station")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_station_ts ON requests(station, timestamp)")
    except Exception as e:
        failed.append("idx_requests_station_ts")
        _record_maintenance_error("index idx_requests_station_ts", e)
    conn.commit()
    # [30] Canary: mixed naive/UTC timestamps break ORDER BY timestamp DESC.
    # BORNÉ : échantillon LIMIT au lieu de COUNT(*) full-scan (P0-4).
    try:
        rows = conn.execute(
            "SELECT 1 FROM requests WHERE timestamp NOT LIKE '%Z' LIMIT 1001"
        ).fetchall()
        naive = len(rows)
        capped = naive == 1001
        if capped:
            logger.warning("[db] canary timestamps naïfs saturé (≥1001) — lancer scripts/migrate_timestamps_utc.py")
    except Exception as e:
        _record_maintenance_error("canary", e)
        naive = 0
        capped = False
    if report is not None:
        report.update(
            {
                "added_columns": added,
                "existing_columns": existing,
                "failed_ops": failed,
                "canary_capped": capped,
            }
        )
    return naive


def init_requests_schema(conn: sqlite3.Connection, *, busy_timeout: int, cache_size: int, mmap_size: int) -> int:
    """PRAGMAs WAL/NORMAL + schéma requests + migrations colonnes + index.

    Wrapper synchrone historique (contrat tests/test_phase0_contracts.py :
    retourne int) = fast + migrate_and_canary en une fois. Le boot, lui,
    appelle :func:`init_requests_schema_fast` puis :func:`migrate_and_canary`
    en tâche de fond.
    """
    init_requests_schema_fast(conn, busy_timeout=busy_timeout, cache_size=cache_size, mmap_size=mmap_size)
    return migrate_and_canary(conn)


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
    except Exception as e:
        if "duplicate column name" not in str(e).lower():
            _record_maintenance_error("free_model_usage addcol ip", e)
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
    counter_fn=None,
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

    [Phase 8 plan boot] ``counter_fn(model, timestamp, tokens_input,
    tokens_output, tokens_cache)`` est appelé pour chaque ligne `requests`
    insérée : l'hôte y branche l'incrément de ``token_counters_daily``. DI
    volontaire (ce module reste pur) et fail-soft (une erreur de compteur ne
    doit jamais faire échouer l'insertion de la requête).
    """
    if not batch:
        return 0
    inserted = 0
    with lock:
        for item in batch:
            try:
                # [fix Phase 8] La détection du tuple taggé ne doit PAS se fier
                # au seul « item[0] est une str » : un tuple SQL brut commence
                # par l'id de requête, qui EST une chaîne → il était pris pour
                # un (table, payload) et l'INSERT échouait silencieusement
                # (item « sauté », batch perdu). On n'accepte donc comme tag
                # que les noms de tables réellement gérés.
                if isinstance(item, tuple) and len(item) == 2 and item[0] in _TAGGED_TABLES:
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
                if counter_fn is not None:
                    try:
                        # Layout _INSERT_REQUESTS_SQL : (1) timestamp, (2) model,
                        # (5) tokens_input, (6) tokens_output, (7) tokens_cache.
                        counter_fn(row[2], row[1], row[5], row[6], row[7])
                    except Exception:
                        pass
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
        except Exception as e:
            _record_maintenance_error("VACUUM", e)


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
        cutoff_delete = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delete_after_days * 86400))
        cursor = conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff_delete,))
        deleted = cursor.rowcount
        cursor2 = conn.execute("DELETE FROM free_model_usage WHERE timestamp < ?", (cutoff_delete,))
        deleted2 = cursor2.rowcount

        # Phase 2: Nullify bodies for 7-30 day old rows
        cutoff_null = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400))
        cursor3 = conn.execute(
            "UPDATE requests SET request_body = NULL, response_body = NULL "
            "WHERE timestamp < ? AND (request_body IS NOT NULL OR response_body IS NOT NULL)",
            (cutoff_null,),
        )
        cleaned = cursor3.rowcount

        total = deleted + deleted2 + cleaned
        if total > 0:
            conn.commit()
            debug_fn(f"  [db] cleanup: deleted {deleted}+{deleted2} old rows, cleared bodies from {cleaned} requests")
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

# [P2-11] Archivage mensuel auto (défaut 60 j < purge 90 j) : les lignes
# 60-90 j sont DÉPLACÉES vers logs/archive/requests-YYYY-MM.db AVANT que la
# purge ne les supprime — conservation sans croissance du live. 0/None = off.
ARCHIVE_AFTER_DAYS = 60
ARCHIVE_DIRNAME = "archive"

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
        (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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


def archive_old_rows(
    conn: sqlite3.Connection, db_path: str, days: int = ARCHIVE_AFTER_DAYS
) -> tuple[int, list[str]]:
    """Déplace les lignes > ``days`` jours vers logs/archive/requests-YYYY-MM.db.

    [P2-11] Pendant automatisé de scripts/archive_db.py (même format de
    fichiers, mêmes requêtes par mois) pour la maintenance hebdo : sans
    copie de sécurité (la purge hebdo supprime déjà sans backup — archiver
    avant purger est strictement plus sûr), mais avec vérification par mois
    (INSERT puis COUNT avant DELETE du mois ; mois en échec ignoré, jamais
    supprimé du live). Idempotent (INSERT OR REPLACE + re-run sans effet).

    Suppose le lock writer détenu. ``days <= 0``/None = no-op.
    Retourne (lignes déplacées, mois ["YYYY-MM", ...]).
    """
    import datetime as _dt
    import os as _os

    if not days or days <= 0 or not db_path:
        return 0, []
    archive_dir = _os.path.join(_os.path.dirname(db_path), ARCHIVE_DIRNAME)
    try:
        _os.makedirs(archive_dir, exist_ok=True)
    except OSError as e:
        _record_maintenance_error("archive mkdir", e)
        return 0, []
    cutoff = (
        (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    try:
        months = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT substr(timestamp, 1, 7) FROM requests WHERE timestamp < ? ORDER BY 1",
                (cutoff,),
            ).fetchall()
            if r[0]
        ]
    except Exception as e:
        _record_maintenance_error("archive plan", e)
        return 0, []
    try:
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'"
        ).fetchone()
        col_names = [r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()]
    except Exception as e:
        _record_maintenance_error("archive schema", e)
        return 0, []
    if not schema_row or not schema_row[0] or not col_names:
        return 0, []
    table_ddl = schema_row[0].replace("CREATE TABLE requests", "CREATE TABLE IF NOT EXISTS arch.requests", 1)
    if "arch.requests" not in table_ddl:
        _record_maintenance_error("archive ddl", ValueError(f"DDL inattendue: {schema_row[0][:80]}"))
        return 0, []
    col_list = ", ".join(col_names)
    moved_months: list[str] = []
    moved = 0
    for month in months:
        target = _os.path.join(archive_dir, f"requests-{month}.db")
        try:
            year, mon = int(month[:4]), int(month[5:7])
            first = _dt.date(year, mon, 1)
            upper = (first + _dt.timedelta(days=32)).replace(day=1).strftime("%Y-%m-01")
            first_day = first.strftime("%Y-%m-01")
        except ValueError as e:
            _record_maintenance_error(f"archive month {month}", e)
            continue
        try:
            conn.execute("ATTACH DATABASE ? AS arch", (target,))
            try:
                conn.execute(table_ddl)
                conn.commit()
                conn.execute(
                    f"INSERT OR REPLACE INTO arch.requests ({col_list})"
                    f" SELECT {col_list} FROM main.requests"
                    " WHERE timestamp >= ? AND timestamp < ? AND timestamp < ?",
                    (first_day, upper, cutoff),
                )
                conn.commit()
                n = conn.execute(
                    "SELECT COUNT(*) FROM arch.requests WHERE timestamp >= ? AND timestamp < ?",
                    (first_day, upper),
                ).fetchone()[0]
                conn.execute("CREATE INDEX IF NOT EXISTS arch.idx_archive_timestamp ON requests(timestamp)")
                conn.commit()
                conn.execute("DELETE FROM main.requests WHERE timestamp >= ? AND timestamp < ?", (first_day, upper))
                conn.commit()
                moved += int(n or 0)
                moved_months.append(month)
            finally:
                try:
                    conn.execute("DETACH DATABASE arch")
                except sqlite3.Error:
                    pass
        except Exception as e:
            _record_maintenance_error(f"archive {month}", e)
            try:
                conn.rollback()
            except Exception:
                pass
            continue
    if moved:
        logger.info("[db] archive: %d lignes → %s (%s)", moved, archive_dir, ",".join(moved_months))
    return moved, moved_months


def weekly_maintain(
    conn: sqlite3.Connection, lock, purge_days: int = WEEKLY_PURGE_DAYS, *, archive_days: int = 0, db_path: str = ""
) -> float:
    """Checkpoint TRUNCATE + archivage + purge > ``purge_days`` jours + VACUUM.

    [P2-11] ``archive_days`` (> 0 + ``db_path``) déplace d'abord les lignes
    anciennes vers logs/archive/ (conservation) avant que la purge ne
    supprime le reste. ``purge_days`` à 0/None désactive la purge (la
    maintenance redevient checkpoint+VACUUM+archivage).

    Retourne la taille DB en Mo (pour le log de l'app hôte)."""
    with lock:
        # [P2-11] archiver AVANT purger : les lignes 60-90 j sont conservées
        # (fichiers mensuels) au lieu d'être supprimées par la purge 90 j.
        if archive_days and archive_days > 0 and db_path:
            try:
                archive_old_rows(conn, db_path, int(archive_days))
                conn.commit()
            except Exception as e:
                _record_maintenance_error("archive", e)
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
    "ARCHIVE_AFTER_DAYS",
    "ARCHIVE_DIRNAME",
    "DAY_BUCKET_VERSION",
    "DB_RAW_SIZE_CAP",
    "MAX_BODY_STORAGE",
    "BatchState",
    "DbRowRaw",
    "backfill_token_counters",
    "bump_token_counters",
    "cleanup_old_bodies",
    "compact_body_stub",
    "archive_old_rows",
    "db_maintenance_errors",
    "execute_batch_sync",
    "flush",
    "get_meta",
    "init_free_usage_schema",
    "init_requests_schema",
    "init_token_counters_schema",
    "insert_sync",
    "materialize_db_row",
    "normalize_timestamp_utc",
    "quick_body_size",
    "restore_token_counters",
    "set_meta",
    "truncate_body_for_storage",
    "vacuum_if_needed",
    "wal_checkpoint",
    "weekly_maintain",
]
