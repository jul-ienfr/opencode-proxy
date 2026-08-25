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
    monkeypatch.setattr(_cfg, "yaml_get", lambda section, key=None, default=None: default)
    # Reset the hot-reload bookkeeping so every test starts fresh.
    monkeypatch.setattr(_cfg, "_custom_routes_mtime", 0.0)
    monkeypatch.setattr(_cfg, "_custom_routes_last_check", 0.0)
    # Isolate the route dicts from whatever earlier modules left behind.
    monkeypatch.setattr(_cfg, "CUSTOM_ROUTES", {})
    monkeypatch.setattr(_cfg, "ROUTES", {})
    return routes_file


def test_reload_picks_up_new_routes(isolated_routes):
    isolated_routes.write_text(
        json.dumps({"kimi": {"match": ["kimi-k2.6"], "model": "glm-5.1"}}), encoding="utf-8"
    )
    _cfg.maybe_reload_custom_routes()
    assert _cfg.ROUTES.get("kimi", {}).get("model") == "glm-5.1"
    assert _cfg.CUSTOM_ROUTES.get("kimi", {}).get("match") == ["kimi-k2.6"]


def test_reload_removes_deleted_routes(isolated_routes, monkeypatch):
    monkeypatch.setattr(_cfg, "_CUSTOM_ROUTES_CHECK_INTERVAL", 0.0)
    isolated_routes.write_text(json.dumps({"a": {"match": ["a"], "model": "m1"}}), encoding="utf-8")
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


def test_reload_rate_limited(isolated_routes, monkeypatch):
    _cfg.maybe_reload_custom_routes()  # arms the 5 s rate limit
    isolated_routes.write_text(
        json.dumps({"kimi": {"match": ["kimi-k2.6"], "model": "glm-5.1"}}), encoding="utf-8"
    )
    # Force mtime and last_check to be older (NTFS may coalesce, and rate limit would block)
    monkeypatch.setattr(_cfg, "_custom_routes_mtime", 0.0)
    monkeypatch.setattr(_cfg, "_custom_routes_last_check", 0.0)
    _cfg.maybe_reload_custom_routes()  # < 5 s later but file changed → MUST reload (direct)
    assert _cfg.CUSTOM_ROUTES.get("kimi", {}).get("match") == ["kimi-k2.6"]
    assert _cfg.ROUTES.get("kimi", {}).get("model") == "glm-5.1"


def test_reload_unchanged_mtime_is_noop(isolated_routes, monkeypatch):
    monkeypatch.setattr(_cfg, "_CUSTOM_ROUTES_CHECK_INTERVAL", 0.0)
    _cfg.maybe_reload_custom_routes()  # loads {} and records the mtime
    before = dict(_cfg.ROUTES)
    assert _cfg.CUSTOM_ROUTES == {}
    _cfg.maybe_reload_custom_routes()  # same mtime → no-op
    assert _cfg.ROUTES == before
    assert _cfg.CUSTOM_ROUTES == {}


# ── Tests audit 2026-08-25 (fixes B1/B2 + ROUTE_VERSION) ─────────────


@pytest.fixture
def isolated_yaml(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a tmp YAML with an empty custom_routes section."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("custom_routes: {}\n", encoding="utf-8")
    monkeypatch.setattr(_cfg, "CONFIG_PATH", str(yaml_file))
    monkeypatch.setattr(_cfg, "_config_yaml_mtime", 0.0)
    monkeypatch.setattr(_cfg, "_custom_routes_mtime", 0.0)
    monkeypatch.setattr(_cfg, "_custom_routes_last_check", 0.0)
    # Isolate in-memory state: fresh dicts (identity preserved for reload).
    monkeypatch.setattr(_cfg, "CUSTOM_ROUTES", {})
    monkeypatch.setattr(_cfg, "ROUTES", {})
    return yaml_file


def test_save_custom_routes_total_replacement_no_resurrection(isolated_yaml):
    """[B2] Une route supprimée côté GUI ne doit PAS ressusciter dans le YAML."""
    _cfg.save_custom_routes(
        {
            "a": {"match": ["model-a"], "model": "mimo-v2.5-free"},
            "b": {"match": ["model-b"], "model": "glm-5.1"},
        }
    )
    import yaml as _yaml

    with open(isolated_yaml, encoding="utf-8") as f:
        on_disk = _yaml.safe_load(f)
    assert set(on_disk["custom_routes"]) == {"a", "b"}

    # Le GUI renvoie l'état complet SANS la route b → b doit disparaître du disque.
    _cfg.save_custom_routes({"a": {"match": ["model-a"], "model": "mimo-v2.5-free"}})
    with open(isolated_yaml, encoding="utf-8") as f:
        on_disk = _yaml.safe_load(f)
    assert set(on_disk["custom_routes"]) == {"a"}
    assert "b" not in _cfg.CUSTOM_ROUTES

    # Cas extrême : vidage complet → section vide sur disque (pas de résurrection).
    _cfg.save_custom_routes({})
    with open(isolated_yaml, encoding="utf-8") as f:
        on_disk = _yaml.safe_load(f)
    assert not on_disk.get("custom_routes")
    assert _cfg.CUSTOM_ROUTES == {}


def test_route_version_bumped_on_mutations(isolated_yaml):
    """ROUTE_VERSION s'incrémente à chaque mutation effective des routes."""
    v0 = _cfg.ROUTE_VERSION
    _cfg.save_custom_routes({"a": {"match": ["model-a"], "model": "glm-5.1"}})
    v1 = _cfg.ROUTE_VERSION
    assert v1 > v0

    # Reload effectif (fichier modifié) → bump
    import time as _time

    import yaml as _yml

    _time.sleep(0.01)  # mtime granularity
    with open(isolated_yaml, encoding="utf-8") as f:
        doc = _yml.safe_load(f) or {}
    doc.setdefault("custom_routes", {})["c"] = {
        "match": ["model-c"],
        "model": "mimo-v2.5-free",
    }
    with open(isolated_yaml, "w", encoding="utf-8") as f:
        _yml.safe_dump(doc, f)
    _cfg._config_yaml_mtime -= 1  # ≠ mtime réel → force le re-read du YAML
    _cfg._custom_routes_last_check = 0.0  # désarme le rate-limit
    _cfg.maybe_reload_custom_routes()
    assert _cfg.ROUTE_VERSION > v1
    assert _cfg.CUSTOM_ROUTES.get("c", {}).get("model") == "mimo-v2.5-free"


def test_save_env_rebuilds_sorted_routes_and_bumps():
    """[B4] save_env doit rebâtir SORTED_ROUTES et invalider les caches."""
    old_sorted = _cfg.SORTED_ROUTES
    old_version = _cfg.ROUTE_VERSION
    _cfg.save_env({"OPUS_MAP_MODEL": _cfg.load_routes().get("opus", {}).get("model", "kimi-k2.6")})
    try:
        assert _cfg.SORTED_ROUTES is not None
        assert isinstance(_cfg.SORTED_ROUTES, list)
        assert _cfg.ROUTE_VERSION > old_version
    finally:
        _cfg.SORTED_ROUTES = old_sorted


def test_warm_cache_reroutes_immediately_on_inprocess_change(isolated_yaml):
    """[B1 côté in-process] Un cache chaud re-route dès la mutation in-process."""
    import opencode as _oc

    _oc._route_cache.clear()
    _oc._seen_route_version = -1
    try:
        first = _oc._route_for("zz-model-warm")
        assert first is None  # pas de route encore

        _cfg.save_custom_routes(
            {"warm": {"match": ["zz-model-warm"], "model": "glm-5.1"}}
        )

        second = _oc._route_for("zz-model-warm")
        assert second is not None and second.get("model") == "glm-5.1"
    finally:
        _oc._route_cache.clear()
        _oc._seen_route_version = -1


def test_external_edit_detected_after_rate_limit_window(isolated_yaml, monkeypatch):
    """[B1 côté externe] Édition directe de config.yaml : le cache chaud est
    invalidé par le polling mtime dès que la fenêtre de rate-limit passe.

    Couvre exactement le trou du bug d'origine : le lookup cache AVANT le
    check de reload faisait qu'un modèle déjà vu ne re-route jamais."""
    import time as _time

    import opencode as _oc

    _oc._route_cache.clear()
    _oc._seen_route_version = -1
    monkeypatch.setattr(_cfg, "_CUSTOM_ROUTES_CHECK_INTERVAL", 0.0)
    try:
        assert _oc._route_for("zz-model-ext") is None

        _time.sleep(0.01)  # mtime granularity NTFS
        import yaml as _yml

        with open(isolated_yaml, encoding="utf-8") as f:
            doc = _yml.safe_load(f) or {}
        doc.setdefault("custom_routes", {})["ext"] = {
            "match": ["zz-model-ext"],
            "model": "glm-5.1",
        }
        with open(isolated_yaml, "w", encoding="utf-8") as f:
            _yml.safe_dump(doc, f)

        # Requête suivante : le reload doit passer AVANT le lookup cache.
        res = _oc._route_for("zz-model-ext")
        assert res is not None and res.get("model") == "glm-5.1"
    finally:
        _oc._route_cache.clear()
        _oc._seen_route_version = -1
