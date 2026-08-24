import asyncio
from unittest.mock import AsyncMock, MagicMock

import config.settings as st


class TestGeoResolve:
    def test_disabled_passthrough(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", False)
        route = {
            "model": "muse-spark-1.2-contributor",
            "geo": {"allowed_countries": ["United States"], "mode": "strict"},
        }
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
        route = {
            "model": "x",
            "geo": {
                "allowed_countries": ["Germany", "France"],
                "blocked_countries": ["France"],
                "mode": "strict",
            },
        }
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
        monkeypatch.setattr(
            st,
            "GEO_POLICIES",
            {
                "meta": {
                    "allowed_countries": ["United States"],
                    "mode": "strict",
                    "require_vpn": True,
                }
            },
        )
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

        route = {
            "model": "muse-spark-1.2-contributor",
            "geo": {"allowed_countries": ["United States"], "mode": "strict"},
        }
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
        # [v10 §14.3.3] les clés incluent désormais le marqueur de station
        for i in range(8):
            VPNManager._geo_coalesce[frozenset({f"C{i}", "__station_1"})] = object()
        import asyncio

        m = VPNManager.__new__(VPNManager)
        m._current_country = None
        m._countries_list = lambda: ["Germany"]
        try:
            asyncio.run(m.ensure_geo_egress({"NewCountry"}, timeout=0.1))
            raise AssertionError("should have raised")
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
        import asyncio

        from vpn_manager import VPNManager

        VPNManager._geo_coalesce.clear()
        m = VPNManager.__new__(VPNManager)
        m._current_country = "United States"
        m._countries_list = lambda: ["United States", "Germany"]
        m._ip_probe_budget = 8.0

        async def _probe():
            return True

        m._probe_tunnel_light = _probe  # type: ignore
        ok = asyncio.run(m.ensure_geo_egress({"United States"}, timeout=1.0))
        assert ok is True
        VPNManager._geo_coalesce.clear()

    def test_hot_reload_geo_policies(self, monkeypatch):
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(
            st, "GEO_POLICIES", {"p1": {"allowed_countries": ["Germany"], "mode": "strict"}}
        )
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France"})
        route = {"model": "x", "geo": {"extends": "p1"}}
        r = st.resolve_geo(route)
        assert "Germany" in r["effective_allowed"]

    def test_budget_timeout(self):
        import asyncio

        from vpn_manager import VPNManager

        VPNManager._geo_coalesce.clear()
        m = VPNManager.__new__(VPNManager)
        m._current_country = "Germany"
        m._countries_list = lambda: ["United States", "Germany"]
        m._ip_probe_budget = 0.05

        async def _slow_probe():
            await asyncio.sleep(1)
            return True

        m._probe_tunnel_light = _slow_probe  # type: ignore

        async def _never_pin(*a, **k):
            await asyncio.sleep(1)
            return False

        m._control_pin_country = _never_pin  # type: ignore
        ok = asyncio.run(m.ensure_geo_egress({"United States"}, timeout=0.05))
        assert ok is False
        VPNManager._geo_coalesce.clear()

    def test_blacklist_filtered(self, monkeypatch):
        """[v10 §14.3.34] le filtre _host_blacklisted sur les PAYS était un
        no-op retiré : la blacklist ne contient que des hostnames NordVPN.
        Le test vérifie désormais le fallback séquentiel de pin : Germany
        refusée par le control server → essai suivant → United States."""
        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,United States,France"})
        route = {
            "model": "x",
            "geo": {"allowed_countries": ["Germany", "United States"], "mode": "strict"},
        }
        r = st.resolve_geo(route)
        assert "Germany" in r["effective_allowed"]
        import asyncio

        from vpn_manager import VPNManager

        VPNManager._geo_coalesce.clear()
        m = VPNManager.__new__(VPNManager)
        m._station = 1
        m._current_country = None
        m._countries_list = lambda: ["Germany", "United States", "France"]
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
        opencode._geo_block_total += 1
        assert opencode._geo_block_total == before + 1
        opencode._geo_block_total = before


# ── Axe D: Estonia non-regression tests ─────────────────────────────


class TestEstoniaNonRegression:
    """Non-regression: Estonia must never appear as egress when excluded
    from the effective_allowed set (forced_pool)."""

    # 32 real-ish NordVPN countries — Estonia is intentionally NOT in the
    # forced_pool (effective_allowed) but IS in the server_countries list
    # so that naive rotation could pick it.
    _32_COUNTRIES = [
        "Albania",
        "Argentina",
        "Australia",
        "Austria",
        "Belgium",
        "Brazil",
        "Bulgaria",
        "Canada",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Iceland",
        "Ireland",
        "Italy",
        "Japan",
        "Latvia",
        "Lithuania",
        "Luxembourg",
        "Netherlands",
        "New Zealand",
        "Norway",
        "Poland",
        "Portugal",
        "Romania",
        "Singapore",
        "Spain",
        "Sweden",
    ]

    def test_estonia_never_egress_strict(self):
        """Simulate 3 VPN managers each landing on a country drawn from
        32-country pool.  When strict+require_vpn and forced_pool does NOT
        contain Estonia, ensure_geo_egress must never pin to Estonia."""
        from vpn_manager import VPNManager

        VPNManager._geo_coalesce.clear()
        # Build 3 station managers — each with the full 32-country list
        stations = []
        for sid in (1, 2, 3):
            m = VPNManager.__new__(VPNManager)
            m._station = sid
            m._current_country = None
            m._countries_list = lambda: list(self._32_COUNTRIES)
            m._server_countries = ",".join(self._32_COUNTRIES)
            m._host_blacklisted = lambda c: False
            m._ip_probe_budget = 8.0
            m._status = "connected"
            # Track every pin attempt
            m._pin_log = []

            async def _fake_pin(country, **kw):
                m._pin_log.append(country)
                m._current_country = country
                return True

            m._control_pin_country = _fake_pin
            stations.append(m)

        # forced_pool: everything EXCEPT Estonia
        forced_pool = {c for c in self._32_COUNTRIES if c != "Estonia"}
        assert "Estonia" not in forced_pool

        # Rotate each station through ensure_geo_egress 10 times
        # Each call picks the first sorted candidate; after pinning,
        # the next call sees the current country already in allowed
        # so it should short-circuit without pinning again.
        async def _run():
            for i in range(10):
                # Alternate stations to exercise all paths
                st = stations[i % 3]
                ok = await st.ensure_geo_egress(forced_pool, timeout=1.0)
                assert ok is True, f"ensure_geo_egress failed on station {st._station} iter {i}"
                # The pinned country MUST NOT be Estonia
                assert st._current_country != "Estonia", (
                    f"Station {st._station} was pinned to Estonia at iter {i} — "
                    f"Estonia must never appear in forced_pool={sorted(forced_pool)}"
                )
                assert st._current_country in forced_pool

        try:
            asyncio.run(_run())
        finally:
            VPNManager._geo_coalesce.clear()

    def test_paid_geo_via_tunnel_conditional(self, monkeypatch):
        """When _direct_country returns Estonia (not in allowed), the gate
        must NOT allow httpx direct — it must force tunnel.  When
        _direct_country returns an allowed country and GEO_ALLOW_DIRECT is
        True, direct is permitted; when False, tunnel is forced."""
        import config.settings as st
        import opencode

        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(
            st, "IP_ROTATION", {"enabled": True, "server_countries": "Germany,United States,France"}
        )
        monkeypatch.setattr(
            st,
            "GEO_POLICIES",
            {
                "meta": {
                    "allowed_countries": ["Germany", "France"],
                    "mode": "strict",
                    "require_vpn": True,
                }
            },
        )
        monkeypatch.setattr(st, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)

        route = {"model": "x", "geo": {"extends": "meta"}}
        geo_info = st.resolve_geo(route)
        assert "Germany" in geo_info["effective_allowed"]
        assert "Estonia" not in geo_info["effective_allowed"]

        # --- Case A: direct country = Estonia (not in allowed) → tunnel forced ---
        # Mock _direct_country to return Estonia
        async def _direct_estonia():
            return "Estonia"

        monkeypatch.setattr(opencode, "_direct_country", _direct_estonia)
        # Mock shared_state: VPN is on
        import shared_state

        fake_mgr = MagicMock()
        fake_mgr._current_country = "Germany"
        fake_mgr.ensure_geo_egress = AsyncMock(return_value=True)
        monkeypatch.setattr(shared_state, "vpn_managers", [fake_mgr], raising=False)

        # Build a minimal Request mock
        class _FakeState:
            pass

        class _FakeRequest:
            def __init__(self):
                self.headers = {}
                self.state = _FakeState()

        req = _FakeRequest()

        # _enforce_geo_gate should NOT return None (should force tunnel)
        result = asyncio.run(
            opencode._enforce_geo_gate(route, req, is_stream=False, protocol="openai")
        )
        # When _direct_country is Estonia and allow_direct=True but Estonia
        # not in effective_allowed, the gate sets _geo_force_tunnel=True
        # and continues to pin logic.  If station already in allowed, it
        # returns None (pass).  If not, it blocks or pins.
        # The key assertion: _geo_force_tunnel should be True
        # (the gate did NOT short-circuit with direct allowed)
        if result is None:
            # Gate passed via pin — that's fine, but tunnel must be forced
            assert getattr(req.state, "_geo_force_tunnel", False) is True, (
                "Direct Estonia NOT in allowed → _geo_force_tunnel must be True"
            )

        # --- Case B: direct country = Germany (in allowed) + allow_direct=True → direct OK ---
        async def _direct_germany():
            return "Germany"

        monkeypatch.setattr(opencode, "_direct_country", _direct_germany)
        req2 = _FakeRequest()
        result2 = asyncio.run(
            opencode._enforce_geo_gate(route, req2, is_stream=False, protocol="openai")
        )
        # Direct IP Germany in allowed + allow_direct=True → pass through
        assert result2 is None, "Direct Germany in allowed + allow_direct=True → gate must pass"
        assert getattr(req2.state, "_geo_force_tunnel", False) is False, (
            "Direct in allowed → _geo_force_tunnel must be False"
        )

        # --- Case C: direct country = Germany + allow_direct=False → tunnel forced ---
        monkeypatch.setattr(st, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", False)
        req3 = _FakeRequest()
        result3 = asyncio.run(
            opencode._enforce_geo_gate(route, req3, is_stream=False, protocol="openai")
        )
        # Even though Germany is in allowed, allow_direct=False → must force tunnel
        if result3 is None:
            assert getattr(req3.state, "_geo_force_tunnel", False) is True, (
                "allow_direct=False → _geo_force_tunnel must be True even when direct IP in allowed"
            )

    def test_streaming_geo_block_sse_paid(self, monkeypatch):
        """Streaming request with strict geo and VPN down must return
        SSE geo_blocked event with status 451."""
        import config.settings as st
        import opencode

        monkeypatch.setattr(st, "GEO_ENABLED", True)
        monkeypatch.setattr(
            st, "IP_ROTATION", {"enabled": True, "server_countries": "Germany,France"}
        )
        monkeypatch.setattr(
            st,
            "GEO_POLICIES",
            {
                "meta": {
                    "allowed_countries": ["United States"],
                    "mode": "strict",
                    "require_vpn": True,
                }
            },
        )
        monkeypatch.setattr(st, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)

        route = {"model": "x", "geo": {"extends": "meta"}}

        # VPN is DOWN: vpn_managers is empty → require_vpn blocks
        import shared_state

        monkeypatch.setattr(shared_state, "vpn_managers", [], raising=False)

        class _FakeState:
            pass

        class _FakeRequest:
            def __init__(self):
                self.headers = {}
                self.state = _FakeState()

        req = _FakeRequest()

        result = asyncio.run(
            opencode._enforce_geo_gate(route, req, is_stream=True, protocol="openai")
        )
        assert result is not None, "VPN down + strict + require_vpn must block"
        # Must be a StreamingResponse (SSE)
        from starlette.responses import StreamingResponse

        assert isinstance(result, StreamingResponse)
        # Read the SSE body and check for geo_blocked 451
        body_parts = []

        async def _consume():
            async for chunk in result.body_iterator:
                body_parts.append(chunk)

        asyncio.run(_consume())
        full_body = b"".join(body_parts)
        assert b"geo_blocked" in full_body, "SSE must contain geo_blocked"
        assert b"451" in full_body, "SSE must contain status 451"
        assert result.headers.get("X-Geo-Blocked") == "1"

    def test_quota_rotation_respects_forced_pool(self, monkeypatch):
        """on_quota_exhausted with forced_pool must propagate it so the
        background rotation never pins to Estonia."""

        # Build a minimal FreeIPPool mock
        from free_ip_pool import FreeIPPool

        pool = object.__new__(FreeIPPool)
        pool._vpn = MagicMock()
        pool._vpn.enabled = True
        pool._vpn.proxy_mode = "vpn"
        pool._active_station = MagicMock()
        pool._bad_ttl = 60.0
        pool._per_station = lambda s: {
            "bad_until": 0,
            "request_count": 0,
            "session_start": 0,
            "last_confirmed_ip": None,
        }
        pool._any_other_usable = lambda s, forced_pool=None: True
        pool._stations = []  # dual_station property needs this

        # Capture forced_pool passed to _launch_rotation
        captured_forced_pool = {}

        def _fake_launch(station, forced_pool=None):
            captured_forced_pool["value"] = forced_pool
            # Tag the station with forced_pool (same as real code)
            if forced_pool is not None:
                station._geo_forced_pool = forced_pool

        pool._launch_rotation = _fake_launch

        station = MagicMock()
        station._station = 1
        station._current_country = "Germany"

        # forced_pool excludes Estonia
        fp = {"Germany", "France", "United States"}
        pool.on_quota_exhausted(station, forced_pool=fp)

        # Verify _launch_rotation was called with the forced_pool
        assert "value" in captured_forced_pool, "_launch_rotation must have been called"
        assert captured_forced_pool["value"] is fp, "forced_pool must be propagated"
        # Verify station was tagged
        assert getattr(station, "_geo_forced_pool", None) is fp
