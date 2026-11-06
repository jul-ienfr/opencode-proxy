"""Garde corrélée des timeouts SOCKS5 (événement amont vs proxy mort)."""

from __future__ import annotations

import time
from collections import deque
from types import SimpleNamespace

from free.pool import FreeIPPool, Socks5Endpoint


def _pool(*, timeout_pairs=()):
    pool = object.__new__(FreeIPPool)
    pool._vpn = SimpleNamespace(proxy_mode="socks5", enabled=True)
    first = Socks5Endpoint("127.0.0.1", 11080, label="warp-a", ipv6=True, index=0)
    second = Socks5Endpoint("127.0.0.1", 11085, label="warp-b", ipv6=True, index=1)
    pool._socks5_eps = [first, second]
    pool._socks5_current = first
    pool._socks5_any_other = lambda target: True
    pool._per = {}
    pool._socks5_timeout_log = deque(maxlen=64)
    pool._socks5_timeout_window_s = 60.0
    pool._socks5_timeout_threshold = 3
    now = time.monotonic()
    for sid, ip in timeout_pairs:
        pool._socks5_timeout_log.append((now, sid, ip))
    calls = []
    pool._apply_bad_mark = lambda target, cause: calls.append((target.pid, cause))
    return pool, first, calls


def test_socks5_timeout_bad_marks_outside_upstream_event():
    pool, endpoint, calls = _pool()
    pool.notify_connection_failure(endpoint)
    assert calls == [("warp-a", "timeout")]


def test_socks5_timeout_does_not_bad_mark_during_upstream_event():
    # Deux couples (station, IP) distincts déjà vus : le 3e déclenche la garde.
    pool, endpoint, calls = _pool(timeout_pairs=((1, "10.0.0.1"), (2, "10.0.0.2")))
    pool.notify_connection_failure(endpoint)
    assert calls == []


def test_socks5_timeout_respects_redundancy_floor():
    pool, endpoint, calls = _pool()
    third = Socks5Endpoint("127.0.0.1", 11086, label="warp-c", ipv6=True, index=2)
    fourth = Socks5Endpoint("127.0.0.1", 11087, label="warp-d", ipv6=True, index=3)
    pool._socks5_eps = [endpoint, *pool._socks5_eps[1:], third, fourth]
    busy_until = time.monotonic() + 60.0
    pool._per_station(pool._socks5_eps[1])["bad_until"] = busy_until
    pool._per_station(third)["bad_until"] = busy_until
    # 4 endpoints → plancher 3 ; un seul autre sain → pas de bad-mark.
    pool.notify_connection_failure(endpoint)
    assert calls == []


def test_capacity_snapshot_counts_socks5_endpoints():
    pool, first, _calls = _pool()
    third = Socks5Endpoint("127.0.0.1", 11086, label="warp-c", ipv6=True, index=2)
    pool._socks5_eps = [first, *pool._socks5_eps[1:], third]
    pool._socks5_usable = (
        lambda endpoint, exclude_approaching=False, ignore_latency_cool=False: endpoint.pid
        != "warp-b"
    )
    cap = pool.capacity_snapshot()
    assert cap["configured_stations"] == 3
    assert cap["usable_stations"] == 2
    assert cap["capacity_degraded"] is True
    assert cap["capacity_reason"] == "2/3 endpoints usable"
