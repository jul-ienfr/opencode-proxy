"""History route tests (extracted router)."""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.history_routes import register_history_routes


class FakeCache:
    def __init__(self):
        self.data = {}
        self.invalidated = 0

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=None):
        self.data[key] = value

    def invalidate(self):
        self.invalidated += 1
        self.data.clear()


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE requests (
            id TEXT PRIMARY KEY, timestamp TEXT, model TEXT, original_model TEXT,
            duration_ms INTEGER, tokens_input INTEGER, tokens_output INTEGER,
            tokens_cache INTEGER, success INTEGER, error TEXT, protocol TEXT,
            is_stream INTEGER, thinking TEXT, effort TEXT, client_ip TEXT,
            account_alias TEXT, station INTEGER, free_model_ip TEXT,
            geo_country TEXT, geo_blocked INTEGER, geo_direct_country TEXT,
            geo_direct_ip TEXT, geo_via_vpn INTEGER, geo_allowed TEXT,
            tools TEXT, tools_used TEXT, free_status TEXT, paid_status TEXT,
            client_user_agent TEXT, request_body TEXT, response_body TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO requests(id, timestamp, model, original_model, duration_ms,"
        " tokens_input, tokens_output, tokens_cache, success, error, station,"
        " free_model_ip, tools, tools_used, request_body, response_body)"
        " VALUES('r1','2026-01-01T00:00:00Z','model-a','orig',12,10,20,30,1,NULL,2,"
        " '1.2.3.4','[\"bash\"]','[\"read\"]','{\"a\":1}','{\"b\":2}')"
    )
    conn.commit()
    return conn


def _app(conn, *, cache=None):
    cache = cache or FakeCache()
    filters = {"value": None}
    token_usage = {"model-a": {"input": 1, "output": 2, "cache": 3}}

    async def read_sync(fn):
        return fn(conn)

    async def query_sync(fn):
        return fn()

    app = FastAPI()
    register_history_routes(
        app,
        token_checker=lambda request: None,
        build_where=lambda *args, **kwargs: ("", []),
        db_read_sync=read_sync,
        db_query_sync=query_sync,
        stats_cache=cache,
        free_model_id_set=lambda: {"model-a"},
        filters_cache_getter=lambda: filters["value"],
        filters_cache_setter=lambda value: filters.__setitem__("value", value),
        tools_provider_getter=lambda: (lambda: {"writer-tool"}),
        connection_getter=lambda: conn,
        token_usage_getter=lambda: token_usage,
        token_lock_getter=lambda: __import__("threading").Lock(),
        normalize_date_bound=lambda value, end_of_day=False: value,
        debug=lambda message: None,
    )
    return app, cache, filters, token_usage


def test_history_returns_rows_and_free_marker():
    app, _, _, _ = _app(_db())
    payload = TestClient(app).get("/api/history", params={"limit": 10}).json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["per_page"] == 10
    row = payload["logs"][0]
    assert row["id"] == "r1"
    assert row["is_free_model"] is True
    assert row["tools"] == ["bash"]
    assert row["tools_used"] == ["read"]
    assert row["station"] == 2


def test_request_detail_and_404():
    app, *_ = _app(_db())
    client = TestClient(app)
    payload = client.get("/api/requests/r1").json()
    assert payload["request_body"] == {"a": 1}
    assert payload["response_body"] == {"b": 2}
    assert client.get("/api/requests/missing").status_code == 404


def test_filters_cache_and_delete_invalidates():
    app, cache, filters, token_usage = _app(_db())
    client = TestClient(app)
    first = client.get("/api/history/filters").json()
    second = client.get("/api/history/filters").json()
    assert first == second
    assert filters["value"] == first
    assert first["tools_used"] == ["read", "writer-tool"]
    assert client.delete("/api/history", params={"all": True}).json() == {
        "status": "deleted"
    }
    assert filters["value"] is None
    assert cache.invalidated == 1
    assert token_usage["model-a"] == {"input": 0, "output": 0, "cache": 0}
