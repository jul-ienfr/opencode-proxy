"""test_vpn_next_endpoint.py — /api/vpn/next station=0 reporting (review fix).

The station=0 branch must rotate and then REPORT the station the pool
actually routes through. Before the fix it forced station 1:
``switch_ip()`` rotated the pool's ACTIVE station (which may be station 2),
but the response read station 1's (untouched) IP → ok:false + wrong
station_out — a false negative for the operator.

Verified behaviours (all through the real FastAPI route, fakes injected
into ``shared_state``):
- station=0 with ``pool.active_station = s2`` → rotates s2, reports
  station 2 and the NEW s2 IP.
- station=0 with no active station yet → falls back to station 1.
- an explicit station=1 targets station 1 regardless of the active one.
"""
import sqlite3

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import shared_state
from dashboard.api import register_dashboard


class _FakeMgr:
    """Minimal station manager — the subset /api/vpn/next touches."""
    def __init__(self, station, ip):
        self._station = station
        self.current_ip = ip
        self.current_server = {"name": f"svr{station}"}
        self.rotations = 0

    async def connect_next(self, **kw):
        self.rotations += 1
        self.current_ip = f"10.0.0.{self._station + 2}"   # 1→.3, 2→.4
        return self.current_ip


class _FakePool:
    """FreeIPPool slice: active_station + switch_ip(station=...)."""
    def __init__(self, s1, s2=None):
        self._vpn = s1
        self._vpn2 = s2
        self.active_station = None

    async def switch_ip(self, station=None):
        target = station or self.active_station or self._vpn
        return await target.connect_next()


@pytest.fixture
def app(tmp_path, monkeypatch):
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    register_dashboard(fast, str(tmp_path), sqlite3.connect(":memory:"))

    s1 = _FakeMgr(1, "10.0.0.1")
    s2 = _FakeMgr(2, "10.0.0.2")
    pool = _FakePool(s1, s2)
    monkeypatch.setattr(shared_state, "vpn_manager", s1)
    # vpn_manager_2 / free_ip_pool may not exist yet in a bare import —
    # create them for this test and tear them down after.
    monkeypatch.setattr(shared_state, "vpn_manager_2", s2, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    return fast, s1, s2, pool


def _post(app, station):
    with TestClient(app) as client:
        return client.post("/api/vpn/next", json={"station": station}).json()


# ── station=0: report the ACTIVE station (the review-fixed branch) ──

def test_station0_reports_active_station_2_and_fresh_ip(app):
    """Active station = 2 → station=0 rotates station 2 and reports it
    with its NEW IP. (Pre-fix: reported station 1's unchanged IP as
    ok:false — the regression this test locks.)"""
    fast, s1, s2, pool = app
    pool.active_station = s2
    body = _post(fast, 0)
    assert body["ok"] is True
    assert body["station"] == 2
    assert body["ip"] == "10.0.0.4"
    assert s2.current_ip == "10.0.0.4" and s2.rotations == 1
    assert s1.current_ip == "10.0.0.1" and s1.rotations == 0   # untouched


def test_station0_without_active_falls_back_to_station1(app):
    """No free request routed yet (active_station None) → falls back to
    station 1, same as before dual_station."""
    fast, s1, s2, pool = app
    body = _post(fast, 0)
    assert body["ok"] is True
    assert body["station"] == 1
    assert body["ip"] == "10.0.0.3"
    assert s1.rotations == 1 and s2.rotations == 0


# ── explicit station targets stay explicit ──

def test_explicit_station1_ignores_active_station(app):
    """station=1 must rotate station 1 even when the pool is active on 2."""
    fast, s1, s2, pool = app
    pool.active_station = s2
    body = _post(fast, 1)
    assert body["ok"] is True
    assert body["station"] == 1 and body["ip"] == "10.0.0.3"
    assert s1.rotations == 1 and s2.rotations == 0


def test_explicit_station2(app):
    fast, s1, s2, pool = app
    body = _post(fast, 2)
    assert body["ok"] is True
    assert body["station"] == 2 and body["ip"] == "10.0.0.4"
    assert s2.rotations == 1 and s1.rotations == 0


def test_no_pool_falls_back_to_plain_manager(app, monkeypatch):
    """free_ip_pool absent → legacy branch: station=0 uses station 1."""
    fast, s1, s2, pool = app
    monkeypatch.setattr(shared_state, "free_ip_pool", None)
    body = _post(fast, 0)
    assert body["ok"] is True
    assert body["station"] == 1 and body["ip"] == "10.0.0.3"