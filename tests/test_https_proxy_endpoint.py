"""Static HTTPS CONNECT proxy (third path) tests."""

from __future__ import annotations

import time

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

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
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
