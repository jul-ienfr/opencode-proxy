import asyncio
from unittest.mock import AsyncMock, MagicMock

import config.settings as st
import opencode
import shared_state


class FakeMgr:
    def __init__(self, country="United States", ip="5.5.5.5", station=1):
        self._current_country = country
        self.current_ip = ip
        self._station = station
        self.status = "connected"
        self.socks5_url = f"socks5://127.0.0.1:108{station}"
        self.pid = f"127.0.0.1:108{station}"

        async def _eg(cands, timeout=8):
            return True

        self.ensure_geo_egress = _eg.__get__(self, FakeMgr)


def _fake_request():
    class _S:
        pass

    class _R:
        def __init__(self):
            self.headers = {}
            self.state = _S()

    return _R()


def test_i18n_fr_via():
    fr = opencode._geo_i18n("direct_via_vpn", "fr").format(
        station=1, vpnCountry="United States", vpnIp="5.5.5.5",
        model="muse-spark-1.2-contributor", allowed="US, CA",
        directIp="82.12.34.56", directCountry="France",
    )
    assert "Vous êtes en mode Direct" in fr
    assert "82.12.34.56" in fr and "France" in fr


def test_i18n_en_via():
    en = opencode._geo_i18n("direct_via_vpn", "en").format(
        station=2, vpnCountry="Japan", vpnIp="1.1.1.1",
        model="x", allowed="JP",
        directIp="2.2.2.2", directCountry="France",
    )
    assert "You are in Direct" in en


def test_direct_compatible_no_warning(monkeypatch):
    monkeypatch.setattr(st, "GEO_ENABLED", True)
    monkeypatch.setattr(st, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
    monkeypatch.setattr(st, "IP_ROTATION", {"enabled": True, "server_countries": "France,Germany,United States"})
    monkeypatch.setattr(st, "GEO_POLICIES", {"meta": {"allowed_countries": ["United States", "France"], "mode": "strict", "require_vpn": True}})
    route = {"model": "muse-spark-1.2-contributor", "geo": {"extends": "meta"}}
    monkeypatch.setattr(shared_state, "vpn_managers", [FakeMgr("United States", "5.5.5.5", 1)], raising=False)

    async def _direct():
        return "France"

    monkeypatch.setattr(opencode, "_direct_country", _direct)
    monkeypatch.setattr(opencode, "_get_direct_ip", AsyncMock(return_value="1.2.3.4"))
    opencode._direct_ip_cache["ip"] = "1.2.3.4"
    opencode._direct_country_cache.update({"country": "France", "ip": "1.2.3.4", "ts": 9999999999})
    req = _fake_request()
    res = asyncio.run(opencode._enforce_geo_gate(route, req, is_stream=False, protocol="openai"))
    assert res is None
    assert getattr(req.state, "_geo_via_vpn_while_direct", False) is False
    # headers must not contain warning
    h = opencode._geo_headers(route, pinned=getattr(req.state, "_geo_pinned", False), current_country=getattr(req.state, "_geo_current", None))
    assert "X-Geo-Warning" not in h


def test_direct_incompatible_via_warning_headers_and_state(monkeypatch):
    monkeypatch.setattr(st, "GEO_ENABLED", True)
    monkeypatch.setattr(st, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
    monkeypatch.setattr(st, "IP_ROTATION", {"enabled": True, "server_countries": "France,Germany,United States,Japan"})
    monkeypatch.setattr(st, "GEO_POLICIES", {"meta": {"allowed_countries": ["United States", "Japan"], "mode": "strict", "require_vpn": True}})
    route = {"model": "muse-spark-1.2-contributor", "geo": {"extends": "meta"}}
    fake = FakeMgr("United States", "5.5.5.5", 1)
    monkeypatch.setattr(shared_state, "vpn_managers", [fake], raising=False)
    # also need free_ip_pool for best_station fallback
    fake_pool = MagicMock()
    fake_pool._best_station = MagicMock(return_value=fake)
    monkeypatch.setattr(shared_state, "free_ip_pool", fake_pool, raising=False)

    async def _direct():
        return "France"

    monkeypatch.setattr(opencode, "_direct_country", _direct)
    monkeypatch.setattr(opencode, "_get_direct_ip", AsyncMock(return_value="82.12.34.56"))
    opencode._direct_ip_cache["ip"] = "82.12.34.56"
    opencode._direct_country_cache.update({"country": "France", "ip": "82.12.34.56", "ts": 9999999999})

    async def _run():
        req_inner = _fake_request()
        res_inner = await opencode._enforce_geo_gate(route, req_inner, is_stream=False, protocol="openai")
        assert res_inner is None
        assert getattr(req_inner.state, "_geo_via_vpn_while_direct", False) is True
        assert getattr(req_inner.state, "_geo_direct_country", None) == "France"
        assert getattr(req_inner.state, "_geo_direct_ip", None) == "82.12.34.56"
        ctx = opencode._current_geo.get()
        assert ctx and ctx["via_vpn"] is True
        assert "France" == ctx["direct_country"]
        # headers via middleware auto path: calling _geo_headers should now produce warning because context via true
        h = opencode._geo_headers(route, pinned=True, current_country="United States")
        assert h.get("X-Geo-Warning") == "direct incompatible — tunneled"
        assert h.get("X-Geo-Direct-Country") == "France"
        assert h.get("X-Geo-Direct-Ip") == "82.12.34.56"
        assert "United States" in h.get("X-Geo-Allowed", "")
        return req_inner

    asyncio.run(_run())


def test_unknown_direct_via_warning(monkeypatch):
    monkeypatch.setattr(st, "GEO_ENABLED", True)
    monkeypatch.setattr(st, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
    monkeypatch.setattr(st, "IP_ROTATION", {"enabled": True, "server_countries": "United States,Japan"})
    monkeypatch.setattr(st, "GEO_POLICIES", {"meta": {"allowed_countries": ["United States"], "mode": "strict", "require_vpn": True}})
    route = {"model": "x", "geo": {"extends": "meta"}}
    fake = FakeMgr("United States", "5.5.5.5", 1)
    monkeypatch.setattr(shared_state, "vpn_managers", [fake], raising=False)
    fake_pool = MagicMock()
    fake_pool._best_station = MagicMock(return_value=fake)
    monkeypatch.setattr(shared_state, "free_ip_pool", fake_pool, raising=False)

    async def _direct():
        return "unknown"

    monkeypatch.setattr(opencode, "_direct_country", _direct)
    monkeypatch.setattr(opencode, "_get_direct_ip", AsyncMock(return_value="unknown"))
    opencode._direct_ip_cache["ip"] = "unknown"
    opencode._direct_country_cache.update({"country": "unknown", "ip": "unknown", "ts": 9999999999})
    req = _fake_request()
    asyncio.run(opencode._enforce_geo_gate(route, req, is_stream=False, protocol="openai"))
    assert getattr(req.state, "_geo_via_vpn_while_direct", False) is True
    # i18n unknown message
    msg = opencode._geo_i18n("direct_via_vpn_unknown", "fr").format(station=1, vpnCountry="United States", vpnIp="5.5.5.5", model="x", allowed="United States")
    assert "indéterminée" in msg
