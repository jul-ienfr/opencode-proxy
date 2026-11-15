"""Réglages runtime de la réserve : persistance, précédence, route GUI (022 T018)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free import client_reserve as cr
from free import reserve_settings as rs
from dashboard.routers.free_routes import register_free_status_routes


def _fresh_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "registry.db"
    monkeypatch.setattr(rs, "DEFAULT_DB", db)
    monkeypatch.setattr(cr, "DEFAULT_DB", db)
    monkeypatch.setattr(rs, "_CACHE", None)
    monkeypatch.setattr(cr, "_CACHE", None)
    for name in rs.ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    return db


def test_settings_roundtrip(tmp_path: Path, monkeypatch) -> None:
    db = _fresh_db(tmp_path, monkeypatch)
    assert rs.get_int("target", 6) == 6
    applied = rs.set_many({"target": 4, "min": 1}, db=db)
    assert applied["target"] == "4"
    assert rs.get_int("target", 6) == 4
    assert rs.get_int("min", 2) == 1


def test_invalid_settings_are_rejected(tmp_path: Path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        rs.set_many({"target": 9})
    with pytest.raises(ValueError):
        rs.set_many({"client_budget_pct": 250})


def test_env_wins_over_gui(tmp_path: Path, monkeypatch) -> None:
    db = _fresh_db(tmp_path, monkeypatch)
    rs.set_many({"target": 4}, db=db)
    monkeypatch.setenv("RESERVE_TARGET", "5")
    assert rs.get_int("target", 6) == 5


def test_client_budget_reads_gui_setting(tmp_path: Path, monkeypatch) -> None:
    db = _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("CLIENT_QUOTA_ESTIMATE", "200")
    rs.set_many({"client_budget_pct": 50}, db=db)
    assert cr._client_budget() == 100
    rs.set_many({"probe_budget_pct": 25}, db=db)
    assert cr._probe_budget() == 50


def test_route_get_and_post(tmp_path: Path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    app = FastAPI()
    register_free_status_routes(app, token_checker=lambda request: None)
    client = TestClient(app)
    first = client.get("/api/free/client-reserve")
    assert first.status_code == 200
    assert first.json()["settings"]["target"] == "6"
    posted = client.post("/api/free/client-reserve", json={"target": 4})
    assert posted.status_code == 200
    assert posted.json()["settings"]["target"] == "4"
    bad = client.post("/api/free/client-reserve", json={"target": 99})
    assert bad.status_code == 400
