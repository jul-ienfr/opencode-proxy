"""Unit tests for hot-reload of custom_routes.json ([51]).

The old tests/test_hot_reload.py was a print-based MANUAL script whose
module-level code wrote the production custom_routes.json at pytest
collection time and left the reload bookkeeping stale — any later
`maybe_reload_custom_routes()` call (e.g. from `_route_for`) re-read the
live file and undid other tests' monkeypatches. It now lives at
scripts/hot_reload_check.py (run it manually against a live instance).

These tests exercise the same reload machinery against a tmp file.
"""
import json

import pytest

from config import settings as _cfg


@pytest.fixture
def isolated_routes(tmp_path, monkeypatch):
    """Point custom routes at a tmp file and isolate module route state."""
    routes_file = tmp_path / "custom_routes.json"
    routes_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(_cfg, "CUSTOM_ROUTES_PATH", str(routes_file))
    # load_custom_routes() prefers the YAML config — bypass it so the JSON
    # file is actually read.
    monkeypatch.setattr(_cfg, "yaml_get",
                        lambda section, key=None, default=None: default)
    # Reset the hot-reload bookkeeping so every test starts fresh.
    monkeypatch.setattr(_cfg, "_custom_routes_mtime", 0.0)
    monkeypatch.setattr(_cfg, "_custom_routes_last_check", 0.0)
    # Isolate the route dicts from whatever earlier modules left behind.
    monkeypatch.setattr(_cfg, "CUSTOM_ROUTES", {})
    monkeypatch.setattr(_cfg, "ROUTES", {})
    return routes_file


def test_reload_picks_up_new_routes(isolated_routes):
    isolated_routes.write_text(json.dumps(
        {"kimi": {"match": ["kimi-k2.6"], "model": "glm-5.1"}}),
        encoding="utf-8")
    _cfg.maybe_reload_custom_routes()
    assert _cfg.ROUTES.get("kimi", {}).get("model") == "glm-5.1"
    assert _cfg.CUSTOM_ROUTES.get("kimi", {}).get("match") == ["kimi-k2.6"]


def test_reload_removes_deleted_routes(isolated_routes, monkeypatch):
    monkeypatch.setattr(_cfg, "_CUSTOM_ROUTES_CHECK_INTERVAL", 0.0)
    isolated_routes.write_text(json.dumps(
        {"a": {"match": ["a"], "model": "m1"}}), encoding="utf-8")
    _cfg.maybe_reload_custom_routes()
    assert "a" in _cfg.ROUTES
    assert "a" in _cfg.CUSTOM_ROUTES
    isolated_routes.write_text("{}", encoding="utf-8")
    # Re-arm the mtime check: NTFS timestamps have ~100 ns granularity but
    # the two writes can still land in the same tick — force a re-read.
    monkeypatch.setattr(_cfg, "_custom_routes_mtime", 0.0)
    _cfg.maybe_reload_custom_routes()
    assert "a" not in _cfg.ROUTES
    assert "a" not in _cfg.CUSTOM_ROUTES


def test_reload_rate_limited(isolated_routes):
    _cfg.maybe_reload_custom_routes()  # arms the 5 s rate limit
    isolated_routes.write_text(json.dumps(
        {"kimi": {"match": ["kimi-k2.6"], "model": "glm-5.1"}}),
        encoding="utf-8")
    _cfg.maybe_reload_custom_routes()  # < 5 s later → must NOT reload
    assert _cfg.CUSTOM_ROUTES == {}
    assert "kimi" not in _cfg.ROUTES


def test_reload_unchanged_mtime_is_noop(isolated_routes, monkeypatch):
    monkeypatch.setattr(_cfg, "_CUSTOM_ROUTES_CHECK_INTERVAL", 0.0)
    _cfg.maybe_reload_custom_routes()  # loads {} and records the mtime
    before = dict(_cfg.ROUTES)
    assert _cfg.CUSTOM_ROUTES == {}
    _cfg.maybe_reload_custom_routes()  # same mtime → no-op
    assert _cfg.ROUTES == before
    assert _cfg.CUSTOM_ROUTES == {}
