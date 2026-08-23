"""test_vpn_config_http.py — POST/GET /api/vpn-config station_count hot-reload
(plan 18/08 §4).

[plan 18/08 §4] The GUI dropdown 1-10 posts ``{station_count: N}`` to the
dashboard API. The handler consumes the key BEFORE the config fan-out (it
is a runtime action, never a config update), short-circuits when N already
matches the live registry, and delegates the actual scale-up/down + the
final config.yaml persist to ``opencode._apply_station_count``.

Verified behaviours (through the real FastAPI route — same fixture pattern
as test_vpn_stack_persist.py; ``opencode._apply_station_count`` and
``_persist_vpn_config`` are patched so nothing touches the live system):
- GET echoes ``station_count`` (it now lives in the manager's config).
- POST {station_count: 4} → _apply_station_count(4), station_count never
  reaches update_config/_persist (consumed).
- POST with the CURRENT count → short-circuit: _apply_station_count never
  called; the key is still consumed (never fanned out).
- POST {station_count: "abc"} or 15 → HTTP 400 explicit (plan 18/08 axe 3.3
  — a silent clamp would mask GUI/programmatic errors; 1..10 only).
- regression: POST {dual_station: false} (legacy toggle) is a plain config
  update — no _apply_station_count, value persisted and fanned out.
"""
import sqlite3

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import opencode
import shared_state
import dashboard.api as api
from dashboard.api import register_dashboard


class _FakeMgr:
    """Station manager slice: update_config/get_config (+ station)."""
    def __init__(self, station, station_count=2):
        self._station = station
        self.station_count = station_count
        self.config_updates = []

    async def update_config(self, updates: dict) -> dict:
        self.config_updates.append(dict(updates))
        return {}

    def get_config(self) -> dict:
        return {"enabled": True, "vpn_stack": "auto",
                "station_count": self.station_count}


class _FakePool:
    def __init__(self):
        self.updates = []

    def update_config(self, updates: dict):
        self.updates.append(dict(updates))


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    register_dashboard(fast, str(tmp_path), sqlite3.connect(":memory:"))

    s1 = _FakeMgr(1)
    s2 = _FakeMgr(2)
    pool = _FakePool()
    monkeypatch.setattr(shared_state, "vpn_managers", [s1, s2], raising=False)
    # GET /api/vpn-config echoes the legacy alias (the pre-registry
    # endpoint) — point it at the same fake so it mirrors s1's config.
    monkeypatch.setattr(shared_state, "vpn_manager", s1, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)

    applied = []
    async def _fake_apply(n):
        applied.append(int(n))
    monkeypatch.setattr(opencode, "_apply_station_count", _fake_apply)
    persisted = []
    monkeypatch.setattr(api, "_persist_vpn_config",
                        lambda u: persisted.append(dict(u)))
    return fast, s1, s2, pool, applied, persisted


def _post(app, body):
    with TestClient(app) as client:
        return client.post("/api/vpn-config", json=body).json()


# ── GET echoes station_count ─────────────────────────────────────

def test_get_echoes_station_count(ctx):
    fast, s1, s2, pool, applied, persisted = ctx
    with TestClient(fast) as client:
        resp = client.get("/api/vpn-config").json()
    assert resp["station_count"] == 2


# ── POST {station_count: N} → hot-reload, key consumed ───────────

def test_post_station_count_applies_hot_reload(ctx):
    """4 ≠ 2 active stations → _apply_station_count(4); the key is consumed
    BEFORE the fan-out: neither update_config nor the persist ever see it."""
    fast, s1, s2, pool, applied, persisted = ctx

    resp = _post(fast, {"station_count": 4})

    assert resp["ok"] is True
    assert applied == [4]
    assert s1.config_updates == [{}], "station_count never fanned out"
    assert s2.config_updates == [{}]
    assert pool.updates == [{}]
    assert persisted == [{}], "config.yaml persist got the consumed body"


# ── POST without change → short-circuit ──────────────────────────

def test_post_station_count_same_value_short_circuits(ctx):
    """2 == 2 active stations → no _apply_station_count; the key is still
    consumed (never persisted, never fanned out)."""
    fast, s1, s2, pool, applied, persisted = ctx

    resp = _post(fast, {"station_count": 2})

    assert resp["ok"] is True
    assert applied == []
    assert persisted == [{}]


# ── POST invalid value → HTTP 400 (axe 3.3, plan 18/08) ──────────

def test_post_station_count_non_int_is_400(ctx):
    """A non-integer string is a caller bug: explicit 400 rather than a
    silent docile no-op (the old clamp masked GUI/programmatic errors)."""
    fast, s1, s2, pool, applied, persisted = ctx
    with TestClient(fast) as client:
        resp = client.post("/api/vpn-config", json={"station_count": "abc"})
    assert resp.status_code == 400
    assert "station_count" in resp.json()["error"]
    assert applied == []
    assert persisted == []


def test_post_station_count_out_of_range_is_400(ctx):
    """15 stations is outside 1..10: 400, never a silent clamp to 10."""
    fast, s1, s2, pool, applied, persisted = ctx
    with TestClient(fast) as client:
        resp = client.post("/api/vpn-config", json={"station_count": 15})
    assert resp.status_code == 400
    assert applied == []
    assert persisted == []

    with TestClient(fast) as client:
        resp0 = client.post("/api/vpn-config", json={"station_count": 0})
    assert resp0.status_code == 400


# ── regression: legacy dual_station toggle ───────────────────────

def test_post_dual_station_legacy_is_plain_config(ctx):
    """The old toggle key is NOT the station_count branch: no hot-reload,
    the value is a normal config update (fanned out + persisted)."""
    fast, s1, s2, pool, applied, persisted = ctx

    resp = _post(fast, {"dual_station": False})

    assert resp["ok"] is True
    assert applied == [], "dual_station never triggers _apply_station_count"
    assert s1.config_updates == [{"dual_station": False}]
    assert s2.config_updates == [{"dual_station": False}]
    assert persisted == [{"dual_station": False}]