"""Static HTTPS CONNECT proxy (third path) tests."""

from __future__ import annotations

import time

import pytest

import free.pool as fp


class _Station:
    def __init__(self, sid: int):
        self._station = sid
        self._config = {}
        self.current_ip = "10.0.0.1"
        self.pid = None
        self._quota_per_ip = 500
        self.enabled = True
        self.proxy_mode = "vpn"
        self.status = "connected"
        self.current_server = None
        self._egress_state = "tunnel_up_full"
        self._last_effective_ok_at = time.monotonic()
        self.socks5_url = f"socks5://127.0.0.1:{1080 + sid}"

    def get_status(self) -> dict:
        return {"station": self._station, "status": self.status}

    def note_free_request(self) -> None:
        pass

    def arm_egress_watchdog(self) -> None:
        pass


def _pool() -> fp.FreeIPPool:
    return fp.FreeIPPool(_Station(1))


def test_https_endpoint_builds_a_tls_connect_url():
    ep = fp.HttpsProxyEndpoint("al36.nordvpn.com", 89, username="u@x", password="p:/")
    assert ep.socks5_url == "https://u%40x:p%3A%2F@al36.nordvpn.com:89"
    assert ep.current_ip == "https:al36.nordvpn.com:89"
    assert ep.current_server["tls"] is True
    assert ep.proxy_tls_verify is False


def test_https_and_socks_lists_coexist_and_next_filters_https():
    pool = _pool()
    pool.set_socks5_proxies([{"host": "127.0.0.1", "port": 1080}])
    pool.set_https_proxies(
        [
            {"host": "al36.nordvpn.com", "port": 89, "username": "u", "password": "p"},
            {"host": "ch296.nordvpn.com", "port": 89, "enabled": False},
        ]
    )
    https = [ep for ep in pool._socks5_eps if isinstance(ep, fp.HttpsProxyEndpoint)]
    socks = [ep for ep in pool._socks5_eps if not isinstance(ep, fp.HttpsProxyEndpoint)]
    assert len(https) == 2 and len(socks) == 1
    assert pool._https_next() is https[0]
    assert pool._https_enabled_eps() == [https[0]]


def test_https_state_survives_a_socks_list_rebuild():
    pool = _pool()
    pool.set_https_proxies([{"host": "al36.nordvpn.com", "port": 89}])
    ep = pool._https_next()
    assert ep is not None
    pool._per_station(ep)["request_count"] = 7

    pool.set_socks5_proxies([{"host": "127.0.0.1", "port": 1080}])

    new_ep = pool._https_next()
    assert new_ep is not None
    assert pool._per_station(new_ep)["request_count"] == 7


def test_https_endpoint_resolves_its_real_egress_ip(monkeypatch):
    ep = fp.HttpsProxyEndpoint("al36.nordvpn.com", 89)

    class _Resp:
        text = "87.120.102.14"

    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **k: _Resp())
    assert ep.refresh_egress() == "87.120.102.14"
    assert ep.current_ip == "87.120.102.14"
    assert ep.current_server["egress_ip"] == "87.120.102.14"


def test_invalid_https_rows_are_skipped():
    pool = _pool()
    pool.set_https_proxies(
        [
            {"host": "", "port": 89},
            {"host": "al36.nordvpn.com", "port": 0},
            {"host": "al36.nordvpn.com", "port": 89},
        ]
    )
    assert len(pool._https_enabled_eps()) == 1


def test_socks5_endpoint_resolves_its_real_egress_ip(monkeypatch):
    ep = fp.Socks5Endpoint("socks-nl1.nordvpn.com", 1080)

    class _Resp:
        text = "109.202.99.36"

    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **k: _Resp())
    assert ep.refresh_egress() == "109.202.99.36"
    assert ep.current_ip == "109.202.99.36"
    assert ep.current_server["egress_ip"] == "109.202.99.36"


def test_fallback_treats_excluded_stations_as_unusable():
    pool = _pool()
    station = pool._stations[0]
    assert pool.usable_station_count_excluding(set(), model=None) == 1
    assert pool.usable_station_count_excluding({station}, model=None) == 0


def test_update_config_reads_the_fallback_flags():
    pool = _pool()
    pool.update_config(
        {"socks5_fallback_enabled": True, "https_fallback_enabled": True}
    )
    assert pool._socks5_fallback_enabled is True
    assert pool._https_fallback_enabled is True
    pool.update_config(
        {"socks5_fallback_enabled": False, "https_fallback_enabled": False}
    )
    assert pool._socks5_fallback_enabled is False
    assert pool._https_fallback_enabled is False


def test_socks5_fallback_next_skips_the_https_tier():
    pool = _pool()
    pool.set_socks5_proxies([{"host": "socks-nl1.nordvpn.com", "port": 1080}])
    pool.set_https_proxies([{"host": "uk765.nordvpn.com", "port": 89}])
    ep = pool._socks5_fallback_next()
    assert ep is not None
    assert not isinstance(ep, fp.HttpsProxyEndpoint)
    assert ep.pid == "socks-nl1.nordvpn.com:1080"


@pytest.mark.asyncio
async def test_vpn_mode_falls_back_to_socks5_before_https(monkeypatch):
    pool = _pool()
    pool.set_socks5_proxies([{"host": "socks-nl1.nordvpn.com", "port": 1080}])
    pool.set_https_proxies([{"host": "uk765.nordvpn.com", "port": 89}])
    pool._socks5_fallback_enabled = True
    pool._https_fallback_enabled = True
    monkeypatch.setattr(
        pool, "usable_station_count_excluding", lambda excluded=None, **kwargs: 0
    )

    async def _noop():
        return None

    monkeypatch.setattr(pool, "ensure_connected", _noop)
    ep = pool._socks5_fallback_next()
    assert ep is not None
    monkeypatch.setattr(ep, "refresh_egress", lambda timeout=6.0: "109.202.99.36")
    proxy, station = await pool.on_request()
    assert proxy == ep.socks5_url
    assert station is ep
