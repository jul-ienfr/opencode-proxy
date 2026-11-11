"""Dashboard root route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.root_routes import register_root_route


def _client(index):
    app = FastAPI()
    register_root_route(app, index_loader=lambda: index)
    return TestClient(app)


def test_root_serves_index_html():
    response = _client(b"<html>dash</html>").get("/")
    assert response.status_code == 200
    assert response.text == "<html>dash</html>"
    assert response.headers["content-type"].startswith("text/html")


def test_root_reports_missing_bundle():
    response = _client(None).get("/")
    assert response.status_code == 503
    assert "npm run build" in response.text
    assert response.headers["content-type"].startswith("text/plain")
