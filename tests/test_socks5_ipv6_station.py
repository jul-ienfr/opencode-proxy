"""Station SOCKS5 IPv6 distincte (WARP IPv4 vs IPv6 sur le même host:port)."""

from __future__ import annotations

from free.pool import Socks5Endpoint
from free.quota_ledger import server_ip_key


def test_socks5_usable_bypasses_strict_station_contract(monkeypatch):
    from free.pool import FreeIPPool

    pool = object.__new__(FreeIPPool)
    pool._strict_health = True
    pool._stations = []
    pool._station_health = {}
    pool._max_effective_age_s = 180
    pool._rotation_wait_timeout = 5
    pool._quota_enabled = False
    pool._ledger_enabled = False
    pool._per = {}
    endpoint = Socks5Endpoint(
        "127.0.0.1",
        11080,
        label="warp-ipv6",
        ipv6=True,
        egress_ip="2a09:bac5:32d4:be::13:23e",
    )
    monkeypatch.setattr("free.pool.station_contract", lambda *args, **kwargs: (False, "docker-only"))
    assert pool._socks5_usable(endpoint, exclude_approaching=False) is True


def test_two_labels_same_hostport_are_distinct():
    v4 = Socks5Endpoint("127.0.0.1", 11080, label="warp-ipv4", index=0)
    v6 = Socks5Endpoint(
        "127.0.0.1",
        11080,
        label="warp-ipv6",
        ipv6=True,
        index=1,
    )
    assert v4.pid == "warp-ipv4"
    assert v6.pid == "warp-ipv6"
    assert v4._station != v6._station
    assert v6.ipv6 is True
    assert v6.current_ip == "socks5:warp-ipv6"
    assert v6.get_status()["ipv6"] is True
    assert v4.get_status()["ipv6"] is False


def test_configured_egress_is_used_for_ledger():
    v6 = Socks5Endpoint(
        "127.0.0.1",
        11080,
        label="warp-ipv6",
        ipv6=True,
        egress_ip="2a09:bac5:32d4:be::13:23e",
        index=0,
    )
    assert v6.current_ip == "2a09:bac5:32d4:be::13:23e"
    assert server_ip_key(v6.current_ip) == "2a09:bac5:32d4:be"
    assert v6._egress_probe_attempted is True


def test_no_label_keeps_legacy_pid():
    ep = Socks5Endpoint("127.0.0.1", 11084, index=0)
    assert ep.pid == "127.0.0.1:11084"
    assert ep.current_ip == "socks5:127.0.0.1:11084"
