"""Provider logique des endpoints (feature 007)."""

from __future__ import annotations

import time

import free.pool as fp


class _Station:
    def __init__(self, sid: int = 1):
        self._station = sid
        self._config = {}
        self.current_ip = "10.0.0.1"
        self.pid = None
        self._quota_per_ip = 500
        self.enabled = True
        self.proxy_mode = "socks5"
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


def test_managed_endpoint_defaults_to_warp():
    ep = fp.Socks5Endpoint("127.0.0.1", 11080, label="warp-a", managed=True)
    assert ep.provider == "warp"


def test_unmanaged_endpoint_defaults_to_static():
    ep = fp.Socks5Endpoint("127.0.0.1", 1080, label="proxy-a")
    assert ep.provider == "static"


def test_explicit_provider_is_normalized():
    ep = fp.Socks5Endpoint("127.0.0.1", 11090, label="vps-a", provider="VPS-IPv6")
    assert ep.provider == "vps-ipv6"


def test_block_quota_axis_and_reset_fields():
    ep = fp.Socks5Endpoint(
        "127.0.0.1",
        11090,
        label="vps-a",
        ipv6=True,
        egress_ip="2a09:bac5:32d3:14f0::1",
    )
    assert ep.block_key == "2a09:bac5:32d3:14f0::/64"
    assert ep.quota_axis == "ipv6"
    assert ep.reset_at == 0.0


def test_set_socks5_proxies_preserves_provider():
    pool = fp.FreeIPPool(_Station())
    pool.set_socks5_proxies(
        [
            {
                "host": "127.0.0.1",
                "port": 11080,
                "label": "warp-a",
                "managed": True,
                "provider": "warp",
            },
            {
                "host": "127.0.0.1",
                "port": 1080,
                "label": "proxy-a",
                "managed": False,
                "provider": "proxy_socks5",
            },
        ]
    )
    by_label = {ep.label: ep for ep in pool._socks5_eps}
    assert by_label["warp-a"].provider == "warp"
    assert by_label["proxy-a"].provider == "proxy_socks5"
