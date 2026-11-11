"""SSE route tests (extracted router)."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.event_routes import register_event_routes


class FakeManager:
    def __init__(self, payload="event: test\ndata: {}\n\n"):
        self.payload = payload
        self.unsubscribed = False

    def bind_loop(self, loop):
        return None

    async def subscribe(self):
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(self.payload)
        return queue

    async def unsubscribe(self, queue):
        self.unsubscribed = True


class SaturatedManager(FakeManager):
    async def subscribe(self):
        raise RuntimeError("subscriber limit reached")


def _app(manager):
    app = FastAPI()
    register_event_routes(app, event_manager_getter=lambda: manager)
    return app


def test_route_is_registered():
    paths = {route.path for route in _app(FakeManager()).routes}
    assert "/api/events" in paths


def test_saturated_manager_returns_503():
    response = TestClient(_app(SaturatedManager())).get("/api/events")
    assert response.status_code == 503
    assert response.json()["error"] == "too_many_subscribers"


def test_stream_route_exists():
    paths = {route.path for route in _app(FakeManager()).routes}
    assert "/api/events" in paths
