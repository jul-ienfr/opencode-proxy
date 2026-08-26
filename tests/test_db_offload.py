"""[D1 audit vitesse] Offload sérialisation DB vers le thread writer.

Contrats :
  - ``_save_request`` ne sérialise plus rien de lourd dans l'event loop :
    la queue transporte un objet brut (_DbRowRaw) ;
  - ``_materialize_db_row`` (thread writer) produit EXACTEMENT le même
    tuple SQL qu'avant : dumps tools, dédup tools_used, tronquage ≤100 Ko,
    redaction des secrets — dashboard logs/tokens intacts ;
  - les corps >2 Mo sont remplacés côté caller par un stub compact (la
    queue n'épingle jamais 10 Mo sous stall DB) ;
  - le fallback queue-full matérialise aussi hors loop.
"""

import json

import pytest

import opencode as oc


class TestQuickSize:
    def test_non_dict_is_zero(self):
        assert oc._quick_body_size(None) == 0
        assert oc._quick_body_size("x") == 0

    def test_counts_strings_without_serializing(self):
        body = {
            "model": "glm-5.1",  # 7 chars
            "stream": False,
            "max_tokens": 512,
            "messages": [
                {"role": "user", "content": "x" * 1000},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "y" * 500}],
                },
                {"role": "user", "content": [{"type": "tool_result", "content": "ignored"}]},
            ],
        }
        size = oc._quick_body_size(body)
        assert 1500 <= size <= 1800  # 7 + 1000 + 500 (+256 tool input borne)


class TestCompactStub:
    def test_oversize_body_becomes_small_stub(self):
        body = {
            "model": "m",
            "stream": True,
            "system": "S" * 5000,
            "messages": [{"role": "user", "content": "C" * 3_000_000}],
        }
        assert oc._quick_body_size(body) > oc._DB_RAW_SIZE_CAP
        stub = oc._compact_body_stub(body)
        assert stub["_oversize"] is True
        assert stub["model"] == "m" and stub["stream"] is True
        assert len(json.dumps(stub)) < 2000


class _FakeQueue:
    def __init__(self, sink):
        self.sink = sink

    def put_nowait(self, item):
        self.sink.append(item)

    def qsize(self):
        return len(self.sink)


@pytest.mark.asyncio
async def test_save_request_enqueues_raw_row(monkeypatch):
    """La coroutine appelante ne fait AUCUN dumps/truncate/redact : la queue
    reçoit un _DbRowRaw portant les objets bruts."""
    captured = []
    monkeypatch.setattr(oc, "_db_queue", _FakeQueue(captured))

    body = {"model": "glm-5.1", "messages": [{"role": "user", "content": "hi"}]}
    await oc._save_request(
        "req-d1",
        "glm-5.1",
        "claude-x",
        42,
        10,
        5,
        0,
        protocol="anthropic",
        request_body=body,
        response_body={"choices": []},
        tools=[{"name": "read"}],
        tools_used=["read", "read", "grep"],
    )
    assert len(captured) == 1
    assert captured[0][0] == "requests", "item taggé requests dans la queue writer"
    raw = captured[0][1]
    assert isinstance(raw, oc._DbRowRaw)
    assert raw.request_body is body, "le corps doit voyager BRUT (pas de copie sérialisée)"
    assert raw.tools_used == ["read", "read", "grep"], "dédup effectué au writer, pas au caller"

    row = oc._materialize_db_row(raw)
    assert len(row) == 30
    assert row[0] == "req-d1"
    assert row[16] == json.dumps([{"name": "read"}])
    assert row[17] == json.dumps(["read", "grep"]), "tools_used dédupliqué"
    assert '"model"' in row[18] and "glm-5.1" in row[18]
    assert len(row[18]) <= oc.MAX_BODY_STORAGE + 64  # truncate appliqué


@pytest.mark.asyncio
async def test_materialized_row_redacts_secrets():
    """La redaction B1 s'applique bien dans le thread writer."""
    body = {
        "messages": [
            {"role": "user", "content": "key=sk-ant-api03-AAAAABBBBCCCCDDDDeeeeFFFF"},
        ]
    }
    raw = oc._DbRowRaw(
        ("id",) * 16,
        (None,) * 10,
        request_body=body,
        response_body=None,
        tools=None,
        tools_used=None,
    )
    row = oc._materialize_db_row(raw)
    assert "AAAAABBBB" not in row[18], "secret non masqué"
    assert "***" in row[18]


@pytest.mark.asyncio
async def test_batch_writer_accepts_raw_rows_end_to_end(monkeypatch, tmp_path):
    """Garde-fou régression : _db_execute_batch_sync insère VRAIMENT une
    _DbRowRaw matérialisée dans SQLite (30 colonnes) — pas l'objet brut."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE requests (
            id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL,
            original_model TEXT, duration_ms INTEGER, tokens_input INTEGER,
            tokens_output INTEGER, tokens_cache INTEGER, success INTEGER,
            error TEXT, protocol TEXT, is_stream INTEGER, thinking TEXT,
            effort TEXT, client_ip TEXT, account_alias TEXT, tools TEXT,
            tools_used TEXT, request_body TEXT, response_body TEXT,
            client_user_agent TEXT, free_model_ip TEXT, identity TEXT,
            geo_country TEXT, geo_blocked TEXT, geo_direct_country TEXT,
            geo_direct_ip TEXT, geo_via_vpn TEXT, geo_allowed TEXT, station TEXT
        )
        """
    )
    monkeypatch.setattr(oc, "_conn", conn)

    captured = []

    class _Q:
        def put_nowait(self, item):
            captured.append(item)

        def qsize(self):
            return len(captured)

    monkeypatch.setattr(oc, "_db_queue", _Q())
    await oc._save_request(
        "req-e2e",
        "glm-5.1",
        None,
        7,
        3,
        2,
        0,
        request_body={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert captured[0][0] == "requests"
    assert isinstance(captured[0][1], oc._DbRowRaw)
    n = oc._db_execute_batch_sync(list(captured))
    assert n == 1
    db_row = conn.execute(
        "SELECT id, model, request_body FROM requests WHERE id='req-e2e'"
    ).fetchone()
    assert db_row is not None
    assert db_row[1] == "glm-5.1"
    assert "hello" in db_row[2]


class _RecordingConn:
    """Connexion factice qui enregistre tout accès execute/commit côté loop."""

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, *a, **k):
        self.calls.append("execute")
        raise AssertionError("sqlite exécuté sur la boucle (P1.2 violé)")

    def commit(self):
        self.calls.append("commit")
        raise AssertionError("commit sur la boucle (P1.2 violé)")


@pytest.mark.asyncio
async def test_log_free_model_usage_enqueues_without_sqlite_on_loop(monkeypatch):
    """[P1.2] _log_free_model_usage ne touche JAMAIS sqlite sur la boucle :
    la ligne part dans la queue writer sous forme de tuple taggé."""
    rec = _RecordingConn()
    monkeypatch.setattr(oc, "_conn", rec)

    captured = []
    monkeypatch.setattr(oc, "_db_queue", _FakeQueue(captured))

    oc._log_free_model_usage(
        "claude-sonnet", "glm-5.1", "sk-ant-api03-abcdef1234567890", "wrk_x", 200,
        tokens_in=11, tokens_out=22, duration_ms=33, ip="1.2.3.4",
    )
    assert rec.calls == [], "aucun appel sqlite synchrone sur la boucle"
    assert len(captured) == 1
    table, row = captured[0]
    assert table == "free_usage"
    assert len(row) == 10
    ts, paid, free, key_masked, ws, status, t_in, t_out, dur, ip = row
    assert free == "glm-5.1" and paid == "claude-sonnet"
    assert key_masked == "sk-ant-api03-abc...", "clé tronquée à 16 chars + ellipsis"
    assert "sk-ant-api03-abcdef1234567890" not in key_masked, "clé pleine jamais en queue"
    assert status == 200 and t_in == 11 and t_out == 22 and dur == 33 and ip == "1.2.3.4"
    assert ts.endswith("Z")


@pytest.mark.asyncio
async def test_free_usage_tagged_row_inserts_end_to_end(monkeypatch):
    """Garde-fou régression P1.2 : le writer batché insère VRAIMENT un tuple
    taggé ("free_usage", row) dans la table free_model_usage."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(_FREE_USAGE_DDL)
    monkeypatch.setattr(oc, "_conn", conn)

    batch = [
        ("free_usage",
         ("2026-08-26T10:00:00Z", "claude-sonnet", "glm-5.1", "sk-...mask",
          "wrk_x", 200, 5, 7, 42, "1.2.3.4")),
        ("requests", ("id-r",) + ("x",)) ,  # type ignoré : table absente → item sauté fail-soft
    ]
    # La ligne requests ci-dessus vise une table inexistante : le contrat
    # fail-soft du writer veut qu'elle soit sautée SANT casser le batch.
    n = oc._db_execute_batch_sync(batch)
    assert n == 1, "seul l'item free_usage valide est compté"
    rows = conn.execute(
        "SELECT timestamp, paid_model, free_model, api_key, workspace_id, status,"
        " tokens_input, tokens_output, duration_ms, ip FROM free_model_usage"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "2026-08-26T10:00:00Z"
    assert rows[0][2] == "glm-5.1"
    assert rows[0][9] == "1.2.3.4"


_FREE_USAGE_DDL = """
CREATE TABLE free_model_usage (
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
"""


@pytest.mark.asyncio
async def test_mixed_batch_requests_and_free_usage(monkeypatch):
    """Batch mixte taggé : les deux tables sont alimentées par le même
    passage writer (un seul lock, un seul commit)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE requests (
            id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL,
            original_model TEXT, duration_ms INTEGER, tokens_input INTEGER,
            tokens_output INTEGER, tokens_cache INTEGER, success INTEGER,
            error TEXT, protocol TEXT, is_stream INTEGER, thinking TEXT,
            effort TEXT, client_ip TEXT, account_alias TEXT, tools TEXT,
            tools_used TEXT, request_body TEXT, response_body TEXT,
            client_user_agent TEXT, free_model_ip TEXT, identity TEXT,
            geo_country TEXT, geo_blocked TEXT, geo_direct_country TEXT,
            geo_direct_ip TEXT, geo_via_vpn TEXT, geo_allowed TEXT, station TEXT
        )
        """
    )
    conn.execute(_FREE_USAGE_DDL)
    monkeypatch.setattr(oc, "_conn", conn)

    raw_req = oc._DbRowRaw(
        ("id-mix", "2026-08-26T10:00:00Z", "glm-5.1") + (None,) * 13,
        (None,) * 10,
        request_body=None,
        response_body=None,
        tools=None,
        tools_used=None,
    )
    batch = [
        ("requests", raw_req),
        ("free_usage",
         ("2026-08-26T10:00:01Z", "opus", "minimax-m2.5", "k", "w", 200, 1, 2, 3, "")),
    ]
    n = oc._db_execute_batch_sync(batch)
    assert n == 2
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM free_model_usage").fetchone()[0] == 1
