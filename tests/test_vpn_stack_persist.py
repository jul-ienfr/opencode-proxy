"""test_vpn_stack_persist.py — POST /api/vpn-config persistence order (audit fix).

[audit 18/08] The handler used to persist the WHOLE body (vpn_stack
included) to config.yaml BEFORE calling set_stack(). When set_stack
refused — wireguard without vpn_configs/wireguard.env — config.yaml was
left saying ``vpn_stack: wireguard`` while the effective stack stayed
openvpn: an incoherent persistent state that also killed auto-flips on
reboot (a manual selection never flips on its own).

The fix orders the writes:
1. persist the body WITHOUT vpn_stack (other keys are unconditional),
2. call set_stack() on station 1,
3. persist {"vpn_stack": mode} ONLY if set_stack succeeded,
4. mirror the state into station 2 (propagate=False) ONLY on success —
   a refused flip must not desync the two managers.

Verified behaviours (through the real FastAPI route, fakes injected into
``shared_state``, ``_persist_vpn_config`` monkeypatched to capture calls
so the real repo config.yaml is never touched):
- refusal → persist called once WITHOUT vpn_stack, station 2 untouched.
- success → persist called with the filtered body THEN {"vpn_stack"} and
  station 2 mirrors once.
- body without vpn_stack → persist once with the whole body, set_stack
  never called.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import shared_state
import dashboard.api as api
from dashboard.api import register_dashboard


class _FakeMgr:
    """Station manager slice: update_config/set_stack/get_config."""

    def __init__(self, station, set_result=None):
        self._station = station
        self.set_result = set_result or {"ok": True}
        self.set_calls = []
        self.config_updates = []

    async def update_config(self, updates: dict) -> dict:
        self.config_updates.append(dict(updates))
        return {}

    async def set_stack(self, mode: str, propagate: bool = True) -> dict:
        self.set_calls.append({"mode": mode, "propagate": propagate})
        return dict(self.set_result)

    def get_config(self) -> dict:
        return {"enabled": True, "vpn_stack": "auto"}


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
    # [plan 18/08 §4] the POST handler resolves stations via the
    # vpn_managers registry (legacy vpn_manager/vpn_manager_2 pair kept for
    # the pre-registry endpoints) — feed it the same fakes so the fan-out
    # and mirror semantics are exercised for real.
    monkeypatch.setattr(shared_state, "vpn_managers", [s1, s2], raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager", s1)
    monkeypatch.setattr(shared_state, "vpn_manager_2", s2, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)

    persisted = []
    monkeypatch.setattr(api, "_persist_vpn_config", lambda u: persisted.append(dict(u)))
    return fast, s1, s2, pool, persisted


def _post(app, body):
    with TestClient(app) as client:
        return client.post("/api/vpn-config", json=body).json()


# ── the audit scenario: set_stack refuses → vpn_stack NOT persisted ──


def test_refusal_keeps_old_stack_persisted(ctx):
    """wireguard without a key → set_stack refuses → config.yaml must NOT
    end up saying wireguard: the filtered body is persisted, the stack
    selection never is, and station 2 is never desynced."""
    fast, s1, s2, pool, persisted = ctx
    s1.set_result = {"ok": False, "error": "vpn_configs/wireguard.env missing"}

    resp = _post(fast, {"enabled": True, "vpn_stack": "wireguard"})

    assert resp["ok"] is True
    # one persist call, carrying everything EXCEPT vpn_stack
    assert len(persisted) == 1
    assert persisted[0] == {"enabled": True}
    # the refused mode reached neither manager's set_stack
    assert s1.set_calls == [{"mode": "wireguard", "propagate": True}]
    assert s2.set_calls == []


# ── success → vpn_stack persisted only after set_stack ok ──


def test_success_persists_stack_after_apply(ctx):
    """set_stack ok → second persist call carries vpn_stack; station 2
    mirrors the state once (propagate=False → no second compose)."""
    fast, s1, s2, pool, persisted = ctx

    resp = _post(fast, {"quota_per_ip": 12, "vpn_stack": "wireguard"})

    assert resp["ok"] is True
    assert persisted == [
        {"quota_per_ip": 12},  # unconditional, stack filtered out
        {"vpn_stack": "wireguard"},  # only after set_stack succeeded
    ]
    assert s1.set_calls == [{"mode": "wireguard", "propagate": True}]
    assert s2.set_calls == [{"mode": "wireguard", "propagate": False}]


# ── no vpn_stack in the body → set_stack never called ──


def test_body_without_stack_never_calls_set_stack(ctx):
    fast, s1, s2, pool, persisted = ctx

    resp = _post(fast, {"quota_per_ip": 10})

    assert resp["ok"] is True
    assert persisted == [{"quota_per_ip": 10}]
    assert s1.set_calls == []
    assert s2.set_calls == []
