"""Free-model usage route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.usage_routes import (
    build_usage_payload,
    register_usage_routes,
)

GROUPED = [
    (
        "paid",
        "free-a",
        "key-1",
        "ws-1",
        "1.2.3.4",
        3,
        2,
        1,
        100,
        50,
        "2026-11-10T10:00:00Z",
        "2026-11-10T11:00:00Z",
    ),
    (
        "paid",
        "free-a",
        "key-2",
        "ws-2",
        "5.6.7.8",
        1,
        1,
        0,
        10,
        5,
        "2026-11-10T09:00:00Z",
        "2026-11-10T09:00:00Z",
    ),
]
RECENT = [
    ("2026-11-10T11:00:00Z", "free-a", 200, 20, 10, "1.2.3.4"),
]


def test_build_usage_payload_aggregates_dimensions():
    payload = build_usage_payload(
        days=7,
        grouped_rows=GROUPED,
        recent_rows=RECENT,
        total_count=4,
    )
    assert payload["total_requests"] == 4
    assert payload["by_model"]["free-a"]["requests"] == 4
    assert payload["by_model"]["free-a"]["success"] == 3
    assert payload["by_key"]["key-1"]["tokens_in"] == 100
    assert payload["by_workspace"]["ws-2"]["requests"] == 1
    assert payload["by_ip"]["1.2.3.4"]["requests"] == 3
    assert payload["by_ip"]["1.2.3.4"]["reset_at"] is not None
    assert payload["recent"][0]["tokens"] == 30


def test_build_usage_payload_invalid_timestamp_is_available():
    row = (
        "paid",
        "free-a",
        "k",
        "w",
        "bad-ip",
        1,
        1,
        0,
        1,
        1,
        "x",
        "not-a-date",
    )
    payload = build_usage_payload(
        days=7,
        grouped_rows=[row],
        recent_rows=[],
        total_count=1,
    )
    assert payload["by_ip"]["bad-ip"]["available"] is True
    assert payload["by_ip"]["bad-ip"]["reset_at"] is None


def test_usage_route_uses_injected_reader():
    async def reader(query):
        return GROUPED, RECENT, 4

    app = FastAPI()
    register_usage_routes(app, db_read_sync=reader)
    payload = TestClient(app).get("/api/free-model-usage", params={"days": 3}).json()
    assert payload["days"] == 3
    assert payload["total_requests"] == 4
    assert payload["by_model"]["free-a"]["requests"] == 4
