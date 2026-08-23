"""test_free_model_map_http.py — POST /api/vpn-config free_model_map
hot-reload + persistence (fix 19/08).

[fix 19/08] ``free_model_map`` is a TOP-LEVEL config.yaml key, so the
``_persist_vpn_config`` key_map (an ``ip_rotation`` inner-key remapper)
can never persist it. The POST handler now:

1. pops ``free_model_map`` out of the body BEFORE the fan-out — it is
   consumed, never broadcast to the station managers;
2. mutates the shared dict IN PLACE (``clear()`` + ``update()``) — the
   object ``config/settings.py`` builds and ``config/__init__.py``
   re-exports is the SAME object the router imported by reference, so
   the change is visible to every live request without a proxy restart;
3. syncs ``_yaml_data`` so a later ``yaml_set()``/``save_yaml_config()``
   dump does not revert the disk write;
4. persists the top-level key through ``_persist_free_model_map``.

Covered here (TestClient with the test_vpn_stack_persist fakes plus the
real writer against a tmp config.yaml):
  * clear+update semantics: a partial POST REPLACES the whole map.
  * same-object visibility: ``api.config_settings.FREE_MODEL_MAP`` IS
    ``config.settings.FREE_MODEL_MAP`` after the POST (hot-reload proof)
    and GET /api/config echoes it.
  * consumed, not fanned out: managers' update_config and
    ``_persist_vpn_config`` never see free_model_map as a key.
  * mirror sync: ``_yaml_data["free_model_map"]`` equals the posted map.
  * the real ``_persist_free_model_map`` writes the TOP-LEVEL key and
    leaves the ip_rotation block untouched (config.yaml reachable via a
    monkeypatched ``api.__file__`` — the repo config.yaml is never
    written).
"""

import sqlite3

import pytest
import yaml
from fastapi import FastAPI
from starlette.testclient import TestClient

import config  # the re-exported FREE_MODEL_MAP object
import config.settings as st
import dashboard.api as api
import shared_state
from dashboard.api import register_dashboard


class _FakeMgr:
    """Station manager slice: update_config only (free_model_map must
    never reach it)."""

    def __init__(self, station):
        self._station = station
        self.config_updates = []

    async def update_config(self, updates: dict) -> dict:
        self.config_updates.append(dict(updates))
        return {}

    def get_config(self) -> dict:
        return {"enabled": True}


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
    monkeypatch.setattr(shared_state, "vpn_manager", s1)
    monkeypatch.setattr(shared_state, "vpn_manager_2", s2, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)

    persisted = []
    monkeypatch.setattr(api, "_persist_vpn_config", lambda u: persisted.append(dict(u)))
    fmm_persisted = []
    monkeypatch.setattr(api, "_persist_free_model_map", lambda m: fmm_persisted.append(dict(m)))

    saved = dict(st.FREE_MODEL_MAP)  # restore the live map on exit
    yield fast, s1, s2, pool, persisted, fmm_persisted
    st.FREE_MODEL_MAP.clear()
    st.FREE_MODEL_MAP.update(saved)


def _post(app, body):
    with TestClient(app) as client:
        return client.post("/api/vpn-config", json=body).json()


def _get_config(app):
    with TestClient(app) as client:
        return client.get("/api/config").json()


# ── hot-reload semantics ─────────────────────────────────────────


def test_free_model_map_replaces_map_in_place(ctx):
    """A partial POST replaces the WHOLE map (clear+update — the handler
    is a full-map endpoint, not a merge)."""
    fast, s1, s2, pool, persisted, fmm_persisted = ctx
    posted = {"glm-5.1": "deepseek-v4-flash-free", "kimi-k2.6": "mimo-v2.5-free"}

    resp = _post(fast, {"free_model_map": posted})

    assert resp["ok"] is True
    assert st.FREE_MODEL_MAP == posted, "whole-map replacement"
    assert st.FREE_MODEL_MAP is api.config_settings.FREE_MODEL_MAP, (
        "the shared dict was mutated in place (no rebind)"
    )


def test_free_model_map_visible_without_restart(ctx):
    """GET /api/config reads the same object — a POST then GET proves the
    hot-reload is live (the router config_settings handle sees the new
    map with no restart)."""
    fast, *_ = ctx
    posted = {"minimax-m2.5": "mimo-v2.5-free"}

    _post(fast, {"free_model_map": posted})
    cfg = _get_config(fast)

    assert cfg["free_model_map"] == posted


def test_free_model_map_consumed_not_fanned_out(ctx):
    """free_model_map must never reach the station managers nor the
    generic persist helper (popped before the fan-out)."""
    fast, s1, s2, pool, persisted, fmm_persisted = ctx
    posted = {"qwen3.7-max": "mimo-v2.5-free"}

    resp = _post(fast, {"free_model_map": posted, "quota_per_ip": 9})

    assert resp["ok"] is True
    assert persisted == [{"quota_per_ip": 9}], "free_model_map excluded from _persist_vpn_config"
    assert fmm_persisted == [posted], "free_model_map persisted through its own helper"
    assert s1.config_updates == [{"quota_per_ip": 9}]
    assert s2.config_updates == [{"quota_per_ip": 9}]
    assert pool.updates == [{"quota_per_ip": 9}]
    for u in s1.config_updates + s2.config_updates + pool.updates:
        assert "free_model_map" not in u


def test_free_model_map_mirror_synced(ctx):
    """_yaml_data mirror updated so save_yaml_config() later cannot
    revert the disk write."""
    fast, *_ = ctx
    posted = {"mimo-v2.5": "deepseek-v4-flash-free"}

    _post(fast, {"free_model_map": posted})

    assert st._yaml_data.get("free_model_map") == posted


# ── the real writer (end-to-end against a tmp config.yaml) ───────


def test_persist_free_model_map_writes_top_level_key(tmp_path, monkeypatch):
    """The writer puts free_model_map at the TOP LEVEL — ip_rotation and
    the other sections must survive untouched. ``api.__file__`` is
    monkeypatched so the resolver finds the tmp config.yaml (the repo
    file is never written by tests)."""
    monkeypatch.setattr(api, "__file__", str(tmp_path / "dashboard" / "api.py"))
    repo_yaml = {
        "server": {"host": "0.0.0.0", "port": 4000},
        "ip_rotation": {"enabled": True, "station_count": 3, "vpn_stack": "wireguard"},
        "custom_routes": {"kimik26": {"match": ["kimi-k2.6"], "model": "deepseek-v4-flash"}},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(repo_yaml, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )

    api._persist_free_model_map({"glm-5.1": "deepseek-v4-flash-free"})

    got = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert got["free_model_map"] == {"glm-5.1": "deepseek-v4-flash-free"}
    assert got["ip_rotation"] == repo_yaml["ip_rotation"], "ip_rotation block untouched"
    assert got["custom_routes"] == repo_yaml["custom_routes"], "other sections untouched"
    assert got["server"] == repo_yaml["server"]


def test_persist_free_model_map_missing_file_is_noop(tmp_path, monkeypatch):
    """config.yaml absent → best-effort no-op, no exception."""
    monkeypatch.setattr(api, "__file__", str(tmp_path / "dashboard" / "api.py"))
    api._persist_free_model_map({"glm-5.1": "deepseek-v4-flash-free"})
    assert not (tmp_path / "config.yaml").exists()


def test_persist_free_model_map_identity_is_shared_object():
    """The reference the router imported IS the settings object (same
    id) — the whole hot-reload premise."""
    assert config.FREE_MODEL_MAP is st.FREE_MODEL_MAP
