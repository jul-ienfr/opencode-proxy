"""Rotation qualité SOCKS5 : seuls les WARP managés sont tournés."""

from __future__ import annotations

from types import SimpleNamespace

from free.pool import FreeIPPool, Socks5Endpoint


class _Item:
    tps_n = 5
    ttfb_n = 5


class _Quality:
    def get(self, sid, ip):  # noqa: ANN001
        return _Item()

    def is_slow_lane(self, sid, ip):  # noqa: ANN001
        return True

    def is_degrading(self, sid, ip, pct):  # noqa: ANN001
        return False

    def is_ttfb_high(self, sid, ip, ceiling):  # noqa: ANN001
        return False


def _pool(managed: bool):
    pool = object.__new__(FreeIPPool)
    pool._vpn = SimpleNamespace(enabled=True, proxy_mode="socks5")
    pool._stations = []
    endpoint = Socks5Endpoint(
        "127.0.0.1",
        11080,
        label="warp-quality",
        ipv6=True,
        egress_ip="2a09:bac5:32d4:be::13:23e",
        managed=managed,
        index=0,
    )
    endpoint._quota_per_ip = 500
    pool._socks5_eps = [endpoint]
    pool._socks5_current = endpoint
    pool._socks5_sticky = endpoint
    pool._per = {}
    pool._per_station(endpoint)["request_count"] = 10
    pool._quality_rotation = {
        "enabled": True,
        "min_requests": 10,
        "min_samples": 3,
        "trend_pct": 25.0,
        "ttfb_ceiling_ms": 8000.0,
        "global_slow_guard": 2,
        "max_per_hour": 6,
    }
    pool.quality = _Quality()
    pool.latency_engine = None
    pool._quality_rotations = []
    pool._quality_rot_last_sid = {}
    pool._socks5_usable = lambda endpoint, exclude_approaching=False, ignore_latency_cool=False: True
    calls = []
    pool._request_warp_rotation = lambda station, cause: (
        calls.append((station.pid, cause)) or True
    )
    return pool, endpoint, calls


def test_quality_rotation_requests_managed_warp_rotation():
    pool, endpoint, calls = _pool(managed=True)
    pool.on_quality_completion(endpoint, endpoint.current_ip)
    assert calls == [("warp-quality", "quality")]


def test_quality_rotation_ignores_unmanaged_proxy():
    pool, endpoint, calls = _pool(managed=False)
    pool.on_quality_completion(endpoint, endpoint.current_ip)
    assert calls == []
