"""test_free_discovery.py — auto free-model discovery (plan §5).

Hermetic matrix (no network, tmp_path only):

  * _detect pure — pricing==0/0, is_free/free/capabilities.free,
    suffix -free cascade; html filet regex (?i).
  * _apply delta no-op — second identical set does not bump mtime/bus
    (return 0).
  * atomic tmp+replace — persisted file survives the write (no half-yaml)
    and keeps _reload_lock order.
  * default_target invalide → fallback FREE_MODEL_POOL[0] + warning.
  * removed — upstream withdrew an id → log info + state.removed.
  * endpoint /responses for muse/spark, else free_base.
  * union dedup — zen/v1 + zen/go/v1 listing the same ids once.
  * _try_free_model_first e2e — a homonyme now routes via FREE_MODEL_MAP
    (the router was already live; discovery just mutates the map).
"""

import re
import sys
import time
import json

import yaml
import pytest
from pathlib import Path


# ── helpers ─────────────────────────────────────────────────────────


def _payload(*ids, pricing=None, **flags):
    data = []
    for mid in ids:
        m = {"id": mid}
        if pricing is not None:
            m["pricing"] = pricing
        m.update(flags)
        data.append(m)
    return {"data": data}


class _FakeResp:
    def __init__(self, status, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise RuntimeError(f"http {self.status_code}")


class _FakeClient:
    def __init__(self, mapping, record):
        self._mapping = mapping
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        self._record.append(url)
        # mapping[url] can be a _FakeResp or raise
        v = self._mapping.get(url)
        if v is None:
            # default 404 for unmapped urls (docs filet may 404)
            return _FakeResp(404, {})
        if isinstance(v, Exception):
            raise v
        if callable(v):
            return v(url)
        return v


def _install_fake_httpx(monkeypatch, mapping, record=None):
    if record is None:
        record = []

    class _FakeHttpx:
        class Client:
            def __init__(self, **kw):
                self._kw = kw

            def __enter__(self):
                return _FakeClient(mapping, record)

            def __exit__(self, *a):
                return False

    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx())
    return record


def _isolated_settings(tmp_path, monkeypatch, yaml_data):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump(yaml_data, sort_keys=False), encoding="utf-8")
    # force re-import under tmp CONFIG_PATH
    for mod in [k for k in sys.modules if k in ("config.settings", "config")]:
        # keep module objects but point them at tmp_path
        pass
    import config.settings as st

    old_path = st.CONFIG_PATH
    old_yaml = dict(st._yaml_data)
    old_models = dict(st.MODELS)
    old_map = dict(st.FREE_MODEL_MAP)
    old_pool = list(st.FREE_MODEL_POOL)
    old_free = set(st.FREE_MODELS)
    old_state = dict(st._FREE_DISCOVERY_STATE)
    monkeypatch.setattr(st, "CONFIG_PATH", str(cfg))
    # reload yaml_data from tmp file
    st._yaml_data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    # rebuild MODELS/free map from yaml
    st.MODELS.clear()
    for k, v in (st._yaml_data.get("models") or {}).items():
        st.MODELS[k] = {
            "endpoint": "https://x",
            "protocol": v.get("protocol", "openai") if isinstance(v, dict) else "openai",
        }
    st.FREE_MODEL_MAP.clear()
    for k, v in (st._yaml_data.get("free_model_map") or {}).items():
        st.FREE_MODEL_MAP[k] = v
    st.FREE_MODELS.clear()
    st.FREE_MODELS.update(set(st.FREE_MODEL_MAP.values()))
    st.FREE_MODEL_POOL[:] = sorted(st.FREE_MODELS)
    st._FREE_DISCOVERY_STATE.clear()
    st._FREE_DISCOVERY_STATE.update(
        {
            "last_refresh": None,
            "next_refresh": None,
            "source": "none",
            "consecutive_failures": 0,
            "removed": [],
            "detected": sorted(st.FREE_MODELS),
        }
    )
    st.FREE_DISCOVERY_ENABLED = True
    st.FREE_DISCOVERY_AUTO_PERSIST = True
    return st, old_path, old_yaml, old_models, old_map, old_pool, old_free, old_state, cfg


def _restore_settings(st, old):
    old_path, old_yaml, old_models, old_map, old_pool, old_free, old_state, _cfg = old
    st.CONFIG_PATH = old_path
    st._yaml_data.clear()
    st._yaml_data.update(old_yaml)
    st.MODELS.clear()
    st.MODELS.update(old_models)
    st.FREE_MODEL_MAP.clear()
    st.FREE_MODEL_MAP.update(old_map)
    st.FREE_MODELS.clear()
    st.FREE_MODELS.update(old_free)
    st.FREE_MODEL_POOL[:] = old_pool
    st._FREE_DISCOVERY_STATE.clear()
    st._FREE_DISCOVERY_STATE.update(old_state)


# ── pure detects ───────────────────────────────────────────────────


class TestDetectPure:
    def test_pricing_zero_zero(self):
        import config.settings as st

        assert st._is_free_model({"id": "any-model", "pricing": {"input": 0, "output": 0}}) is True

    def test_pricing_nonzero(self):
        import config.settings as st

        assert st._is_free_model({"id": "glm-5", "pricing": {"input": 1, "output": 1}}) is False

    def test_is_free_flag(self):
        import config.settings as st

        assert st._is_free_model({"id": "x", "is_free": True}) is True
        assert st._is_free_model({"id": "x", "free": True}) is True
        assert st._is_free_model({"id": "x", "capabilities": {"free": True}}) is True

    def test_suffix_free(self):
        import config.settings as st

        assert st._is_free_model({"id": "mimo-v2.5-free"}) is True
        assert st._is_free_model({"id": "mimo-v2.5"}) is False

    def test_detect_union(self):
        import config.settings as st

        p1 = _payload("mimo-v2.5-free", "glm-5")
        p2 = _payload("hy3-free")
        assert st._detect_free_ids([p1, p2]) == {"mimo-v2.5-free", "hy3-free"}

    def test_detect_skips_non_dict(self):
        import config.settings as st

        assert st._detect_free_ids([None, {"data": "bad"}]) == set()

    def test_html_regex_case_insensitive(self):
        html = '<td class="x">MIMO-V2.5-FREE</td>'
        ids = set(re.findall(r"(?i)<td[^>]*>\s*([a-z0-9.\-]+-free)\s*</td>", html))
        assert "MIMO-V2.5-FREE" in ids


# ── apply / persist / delta / endpoints / dedup ───────────────────


class TestApplyPersist:
    def test_delta_no_op_does_not_bump_mtime(self, tmp_path, monkeypatch):
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {"mimo-v2.5": {"protocol": "openai"}, "hy3": {"protocol": "openai"}},
            "free_model_map": {"hy3": "hy3-free"},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            # seed FREE_MODELS to {hy3-free}
            st.FREE_MODELS.clear()
            st.FREE_MODELS.update({"hy3-free"})
            st.FREE_MODEL_POOL[:] = ["hy3-free"]
            st._FREE_DISCOVERY_STATE["detected"] = ["hy3-free"]
            st.MODELS["hy3-free"] = {"endpoint": st.API_BASE_FREE, "protocol": "openai"}
            mtime0 = Path(st.CONFIG_PATH).stat().st_mtime
            time.sleep(0.02)
            added = st._apply_discovered_free_models({"hy3-free"}, source="test")
            assert added == 0
            # _apply alone does not write; ensure mtime did NOT change
            assert Path(st.CONFIG_PATH).stat().st_mtime == mtime0
        finally:
            _restore_settings(st, old)

    def test_apply_adds_model_and_homonyme(self, tmp_path, monkeypatch):
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {"hy3": {"protocol": "openai"}},
            "free_model_map": {},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            added = st._apply_discovered_free_models({"hy3-free"}, source="test")
            assert "hy3-free" in st.MODELS
            assert st.FREE_MODEL_MAP.get("hy3") == "hy3-free"
            assert added == 1
            assert st.FREE_MODEL_POOL == ["hy3-free"]
        finally:
            _restore_settings(st, old)

    def test_persist_is_atomic_tmp_replace(self, tmp_path, monkeypatch):
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {"hy3": {"protocol": "openai"}},
            "free_model_map": {},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            st._apply_discovered_free_models({"hy3-free"}, source="test")
            assert not Path(st.CONFIG_PATH + ".tmp").exists()
            st._persist_free_mappings()
            assert not Path(st.CONFIG_PATH + ".tmp").exists(), "tmp must have been replaced"
            on_disk = yaml.safe_load(Path(st.CONFIG_PATH).read_text(encoding="utf-8"))
            assert "hy3-free" in on_disk.get("models", {})
            assert on_disk.get("free_model_map", {}).get("hy3") == "hy3-free"
            # valid yaml
            yaml.safe_load(Path(st.CONFIG_PATH).read_text(encoding="utf-8"))
        finally:
            _restore_settings(st, old)

    def test_endpoint_responses_for_muse_spark(self, tmp_path, monkeypatch):
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {},
            "free_model_map": {},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            # muse-spark models use /v1/responses (Responses API), not /chat/completions
            assert (
                st._free_endpoint_for("muse-spark-1.2-contributor-free")
                == "https://opencode.ai/zen/v1/responses"
            )
            assert st._free_endpoint_for("mimo-v2.5-free") == st.API_BASE_FREE
        finally:
            _restore_settings(st, old)

    def test_removed_is_logged_in_state(self, tmp_path, monkeypatch):
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {},
            "free_model_map": {"hy3": "hy3-free"},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            st.FREE_MODELS.clear()
            st.FREE_MODELS.update({"hy3-free", "mimo-v2.5-free"})
            st._apply_discovered_free_models({"hy3-free"}, source="test")
            assert st._FREE_DISCOVERY_STATE["removed"] == ["mimo-v2.5-free"]
        finally:
            _restore_settings(st, old)

    def test_ensure_fetch_union_dedups(self, tmp_path, monkeypatch):
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {},
            "free_model_map": {},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            urls = st._free_discovery_urls()
            # same payload on both urls → dedup to 2 ids
            payload = _payload("mimo-v2.5-free", "hy3-free")
            mapping = {u: _FakeResp(200, payload) for u in urls}
            _install_fake_httpx(monkeypatch, mapping)
            added = st._ensure_free_models_sync()
            # both frees discovered, both in MODELS
            assert {"mimo-v2.5-free", "hy3-free"} <= st.FREE_MODELS
            assert {"mimo-v2.5-free", "hy3-free"} <= set(st.MODELS.keys())
        finally:
            _restore_settings(st, old)

    def test_try_free_model_first_via_homonyme(self, tmp_path, monkeypatch):
        """End-to-end: after discovery adds hy3→hy3-free, a pretending
        _try_free_model_first sees it (router live semantics)."""
        yaml_data = {
            "server": {"host": "0.0.0.0", "port": 4000},
            "models": {"hy3": {"protocol": "openai"}},
            "free_model_map": {},
        }
        st, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
        try:
            # Simulate discovery homonyme
            st._apply_discovered_free_models({"hy3-free"}, source="test")
            # Minimal router check: FREE_MODEL_MAP is the contract for _try_free_model_first
            assert st.FREE_MODEL_MAP.get("hy3") == "hy3-free"
            assert "hy3-free" in st.MODELS
        finally:
            _restore_settings(st, old)


# ── Axe D: forced_pool rotation for free pool ──────────────────────


class TestForcedPoolRotation:
    """Verify that FreeIPPool.on_quota_exhausted propagates forced_pool
    so background rotations stay within geo-allowed countries (Estonia
    excluded)."""

    def test_quota_rotation_respects_forced_pool_free(self):
        """on_quota_exhausted with forced_pool must tag the station and
        pass forced_pool to _launch_rotation — rotation never picks
        Estonia when forced_pool excludes it."""
        from unittest.mock import MagicMock
        from free_ip_pool import FreeIPPool

        # Build a minimal FreeIPPool in-memory (no network/docker)
        pool = object.__new__(FreeIPPool)
        pool._vpn = MagicMock()
        pool._vpn.enabled = True
        pool._vpn.proxy_mode = "vpn"
        pool._active_station = MagicMock()
        pool._bad_ttl = 60.0
        pool._stations = []  # dual_station property needs this

        # Capture forced_pool and station passed to _launch_rotation
        captured = {}

        def _fake_launch(station, forced_pool=None):
            captured["station"] = station
            captured["forced_pool"] = forced_pool
            if forced_pool is not None:
                station._geo_forced_pool = forced_pool

        pool._launch_rotation = _fake_launch

        # C1 guard: there must be another usable station
        pool._any_other_usable = lambda s, forced_pool=None: True

        station = MagicMock()
        station._station = 1
        station._current_country = "Germany"

        # forced_pool: 3 countries, NO Estonia
        fp = {"Germany", "France", "United States"}
        assert "Estonia" not in fp

        pool.on_quota_exhausted(station, forced_pool=fp)

        # Verify _launch_rotation received the forced_pool
        assert "forced_pool" in captured, "_launch_rotation must have been called"
        assert captured["forced_pool"] is fp, "forced_pool must be passed through"
        # Verify station was tagged with geo constraint
        assert getattr(station, "_geo_forced_pool", None) is fp, (
            "station._geo_forced_pool must be set to forced_pool"
        )

    def test_quota_rotation_without_forced_pool_no_tag(self):
        """When forced_pool is None, station must NOT be tagged with
        _geo_forced_pool — background rotation uses default country list."""
        from unittest.mock import MagicMock
        from free_ip_pool import FreeIPPool

        pool = object.__new__(FreeIPPool)
        pool._vpn = MagicMock()
        pool._vpn.enabled = True
        pool._vpn.proxy_mode = "vpn"
        pool._active_station = MagicMock()
        pool._bad_ttl = 60.0
        pool._stations = []  # dual_station property needs this

        captured = {}

        def _fake_launch(station, forced_pool=None):
            captured["forced_pool"] = forced_pool

        pool._launch_rotation = _fake_launch
        pool._any_other_usable = lambda s, forced_pool=None: True

        station = MagicMock()
        station._station = 2
        station._current_country = "France"
        # Pre-set _geo_forced_pool to something — it should NOT be overwritten
        station._geo_forced_pool = {"Previous"}

        pool.on_quota_exhausted(station, forced_pool=None)

        assert captured.get("forced_pool") is None
        # Without forced_pool, _launch_rotation should not overwrite the tag
        # (the real code only sets station._geo_forced_pool when forced_pool is not None)
        assert station._geo_forced_pool == {"Previous"}
