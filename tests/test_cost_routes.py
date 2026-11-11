"""Cost route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.cost_routes import register_cost_routes


class FakeDB:
    def __init__(self):
        self.rows = [
            {"model": "paid", "tokens_input": 100, "tokens_output": 20},
            {"model": "free", "tokens_input": 10, "tokens_output": 2},
        ]

    def execute(self, sql, params):
        assert "GROUP BY model" in sql
        return self

    def fetchall(self):
        return self.rows


def _app():
    calls = []

    async def read_sync(fn):
        return fn(FakeDB())

    def compute(rows, pricing):
        calls.append((rows, pricing))
        return {"total_usd": 1.5, "free_saved_usd": 0.5}

    app = FastAPI()
    register_cost_routes(
        app,
        build_where=lambda from_date, to_date, station=None: (
            " WHERE station = ?" if station is not None else "",
            [station] if station is not None else [],
        ),
        db_read_sync=read_sync,
        compute_costs=compute,
        pricing_getter=lambda: {"currency": "USD"},
    )
    return TestClient(app), calls


def test_costs_passes_rows_pricing_and_window():
    client, calls = _app()
    payload = client.get(
        "/api/costs",
        params={"from_date": "2026-01-01", "to_date": "2026-01-02", "station": 2},
    ).json()
    assert payload == {
        "total_usd": 1.5,
        "free_saved_usd": 0.5,
        "window": {"from": "2026-01-01", "to": "2026-01-02", "station": 2},
    }
    assert calls[0][1] == {"currency": "USD"}
    assert len(calls[0][0]) == 2
