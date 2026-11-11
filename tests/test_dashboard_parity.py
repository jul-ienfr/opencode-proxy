"""Dashboard route parity and latency budgets (T166)."""

from __future__ import annotations

import sqlite3
import time
from statistics import quantiles

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import shared_state
from dashboard.api import register_dashboard

SCHEMA = """
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


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    for index in range(100):
        conn.execute(
            "INSERT INTO requests(id, timestamp, model, original_model, duration_ms,"
            " tokens_input, tokens_output, tokens_cache, success, error, station,"
            " account_alias, free_model_ip, tools, tools_used)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"r{index}",
                f"2026-11-10T12:{index % 60:02d}:00Z",
                "glm-5.3-flash",
                "glm-5.3-flash",
                100,
                50,
                20,
                5,
                1,
                None,
                1 + index % 6,
                "main",
                "1.2.3.4" if index % 2 else "",
                "[]",
                "[]",
            ),
        )
    conn.commit()
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(shared_state, "vpn_managers", [], raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager", None, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", None, raising=False)
    monkeypatch.setattr(
        shared_state,
        "shared_rotation",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        shared_state,
        "docker_event_watcher",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        shared_state,
        "station_supervisors",
        [],
        raising=False,
    )
    monkeypatch.setattr(
        shared_state,
        "latency_engine",
        None,
        raising=False,
    )
    app = FastAPI()
    register_dashboard(app, str(tmp_path), conn)
    try:
        yield TestClient(app)
    finally:
        conn.close()


def test_extracted_routes_keep_shape_and_latency(dashboard_client):
    cases = {
        "/api/vpn-status": ("status", "proxy_mode"),
        "/api/history?limit=20": ("logs", "total"),
        "/api/stats": ("totals", "models", "accounts"),
        "/api/costs": ("window",),
        "/api/routes?limit=50": ("routes", "total"),
    }
    for path, keys in cases.items():
        response = dashboard_client.get(path)
        assert response.status_code == 200, path
        payload = response.json()
        for key in keys:
            assert key in payload, (path, key)

        dashboard_client.get(path)  # warm cache
        timings = []
        for _ in range(15):
            started = time.perf_counter()
            assert dashboard_client.get(path).status_code == 200
            timings.append((time.perf_counter() - started) * 1000.0)
        p95 = quantiles(timings, n=20)[18]
        assert p95 < 500.0, f"{path} p95={p95:.1f}ms"
