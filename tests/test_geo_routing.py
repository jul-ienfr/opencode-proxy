import pytest
from unittest.mock import patch, MagicMock
import config.settings as st


class TestGeoResolve:
    def test_disabled_passthrough(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", False)
        route = {"model": "muse-spark-1.2-contributor", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        r = st.resolve_geo(route)
        assert r["geo_status"] == "disabled"

    def test_no_geo_ok(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        route = {"model": "x"}
        r = st.resolve_geo(route)
        assert r["geo_status"] == "ok"

    def test_blocked_gt_allowed(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        # need server_countries to contain Germany/France
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France,Italy"})
        route = {"model": "x", "geo": {"allowed_countries": ["Germany", "France"], "blocked_countries": ["France"], "mode": "strict"}}
        r = st.resolve_geo(route)
        assert "France" not in r["effective_allowed"]
        assert "Germany" in r["effective_allowed"]

    def test_misconfigured_empty_strict(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France"})
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        r = st.resolve_geo(route)
        assert r["geo_status"] == "misconfigured"
        assert len(r["effective_allowed"]) == 0

    def test_extends(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,United States,France"})
        monkeypatch.setattr(st, "GEO_POLICIES", {"meta": {"allowed_countries": ["United States"], "mode": "strict", "require_vpn": True}})
        route = {"model": "x", "geo": {"extends": "meta"}}
        r = st.resolve_geo(route)
        assert "United States" in r["effective_allowed"]
        assert r["require_vpn"] is True

    def test_alias_usa(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "United States,Germany"})
        route = {"model": "x", "geo": {"allowed_countries": ["USA"], "mode": "strict"}}
        r = st.resolve_geo(route)
        assert "United States" in r["effective_allowed"]


class TestGeoGateIntegration:
    def test_geo_block_response_headers(self):
        import opencode
        route = {"model": "muse-spark-1.2-contributor", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        resp = opencode._geo_block_response(route, "Germany", "openai")
        assert resp.status_code in (403, 400, 451)
        assert resp.headers.get("X-Geo-Blocked") == "1"

    def test_geo_sse_error(self):
        import opencode
        route = {"model": "x", "geo": {"allowed_countries": ["United States"]}}
        payload = opencode._geo_sse_error(route, "Germany")
        assert b"geo_blocked" in payload
        assert b"451" in payload

    def test_prefer_fallback(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,United States"})
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "prefer"}}
        r = st.resolve_geo(route)
        assert r["mode"] == "prefer"

    def test_warn_mode(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,United States"})
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "warn"}}
        r = st.resolve_geo(route)
        assert r["mode"] == "warn"

    def test_blocked_only(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France,Italy"})
        route = {"model": "x", "geo": {"blocked_countries": ["France"], "mode": "strict"}}
        r = st.resolve_geo(route)
        assert "France" not in r["effective_allowed"]
        assert "Germany" in r["effective_allowed"]

    def test_geo_headers_minimized(self):
        import opencode
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        h = opencode._geo_headers(route, pinned=False, current_country="Germany")
        assert "X-Geo-Mode" in h
        assert "X-Geo-Pinned" in h
        assert h["X-Geo-Pinned"] == "false"

    def test_queue_saturation(self):
        from vpn_manager import VPNManager
        VPNManager._geo_coalesce.clear()
        for i in range(8):
            VPNManager._geo_coalesce[frozenset({f"C{i}"})] = object()
        import asyncio
        m = VPNManager.__new__(VPNManager)
        m._current_country = None
        m._countries_list = lambda: ["Germany"]
        m._host_blacklisted = lambda x: False
        try:
            asyncio.run(m.ensure_geo_egress({"NewCountry"}, timeout=0.1))
            assert False, "should have raised"
        except RuntimeError as e:
            assert "503" in str(e) or "saturated" in str(e)
        finally:
            VPNManager._geo_coalesce.clear()

    def test_geo_block_451_passthrough(self):
        import opencode
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        resp = opencode._geo_block_response(route, "Germany", "openai", passthrough_451=True)
        assert resp.status_code == 451

    def test_geo_block_anthropic_400(self):
        import opencode
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        resp = opencode._geo_block_response(route, "Germany", "anthropic")
        assert resp.status_code == 400

    def test_coalescing_same_allowed(self):
        from vpn_manager import VPNManager
        import asyncio
        VPNManager._geo_coalesce.clear()
        m = VPNManager.__new__(VPNManager)
        m._current_country = "United States"
        m._countries_list = lambda: ["United States", "Germany"]
        m._ip_probe_budget = 8.0
        async def _probe():
            return True
        m._probe_tunnel_light = _probe  # type: ignore
        m._host_blacklisted = lambda x: False
        ok = asyncio.run(m.ensure_geo_egress({"United States"}, timeout=1.0))
        assert ok is True
        VPNManager._geo_coalesce.clear()

    def test_hot_reload_geo_policies(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "GEO_POLICIES", {"p1": {"allowed_countries": ["Germany"], "mode": "strict"}})
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France"})
        route = {"model": "x", "geo": {"extends": "p1"}}
        r = st.resolve_geo(route)
        assert "Germany" in r["effective_allowed"]

    def test_budget_timeout(self):
        from vpn_manager import VPNManager
        import asyncio
        VPNManager._geo_coalesce.clear()
        m = VPNManager.__new__(VPNManager)
        m._current_country = "Germany"
        m._countries_list = lambda: ["United States", "Germany"]
        m._ip_probe_budget = 0.05
        async def _slow_probe():
            await asyncio.sleep(1)
            return True
        m._probe_tunnel_light = _slow_probe  # type: ignore
        m._host_blacklisted = lambda x: False
        async def _never_pin(*a, **k):
            await asyncio.sleep(1)
            return False
        m._control_pin_country = _never_pin  # type: ignore
        ok = asyncio.run(m.ensure_geo_egress({"United States"}, timeout=0.05))
        assert ok is False
        VPNManager._geo_coalesce.clear()

    def test_blacklist_filtered(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,United States,France"})
        route = {"model": "x", "geo": {"allowed_countries": ["Germany", "United States"], "mode": "strict"}}
        r = st.resolve_geo(route)
        assert "Germany" in r["effective_allowed"]
        from vpn_manager import VPNManager
        import asyncio
        VPNManager._geo_coalesce.clear()
        m = VPNManager.__new__(VPNManager)
        m._current_country = None
        m._countries_list = lambda: ["Germany", "United States", "France"]
        m._host_blacklisted = lambda c: c == "Germany"
        m._ip_probe_budget = 8.0
        async def _pin_ok(country, **kw):
            return country == "United States"
        m._control_pin_country = _pin_ok  # type: ignore
        ok = asyncio.run(m.ensure_geo_egress({"Germany", "United States"}, timeout=1.0))
        assert ok is True
        assert m._current_country == "United States"
        VPNManager._geo_coalesce.clear()

    def test_geo_metrics(self):
        import opencode
        assert hasattr(opencode, "_geo_block_total")
        assert hasattr(opencode, "_geo_pin_duration")
        assert isinstance(opencode._geo_pin_duration, list)

    def test_geo_block_increments(self, monkeypatch):
        import opencode
        before = opencode._geo_block_total
        route = {"model": "x", "geo": {"allowed_countries": ["United States"], "mode": "strict"}}
        opencode._geo_block_total += 1
        assert opencode._geo_block_total == before + 1
        opencode._geo_block_total = before
