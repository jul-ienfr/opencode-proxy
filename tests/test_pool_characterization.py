"""Characterization tests for pool selection/public helpers (T158)."""

from __future__ import annotations

from types import SimpleNamespace

from free.pool import FreeIPPool, HttpsProxyEndpoint, Socks5Endpoint


class _NoHealth:
    def get(self, station, create=False):
        return None


def _dedupe_pool():
    pool = object.__new__(FreeIPPool)
    pool._per = {}
    pool._station_health = _NoHealth()
    return pool


def test_socks5_endpoint_url_escapes_credentials():
    endpoint = Socks5Endpoint(
        "proxy.example",
        1080,
        username="u@x",
        password="p:/",
    )
    assert endpoint.has_password is True
    assert endpoint.socks5_url == "socks5://u%40x:p%3A%2F@proxy.example:1080"


def test_https_endpoint_url_uses_https_scheme():
    endpoint = HttpsProxyEndpoint(
        "proxy.example",
        89,
        username="u",
        password="p",
    )
    assert endpoint.socks5_url == "https://u:p@proxy.example:89"


def test_public_subnet_v4_and_v6():
    assert FreeIPPool._public_subnet("192.0.2.10") == "192.0.2.0/24"
    assert FreeIPPool._public_subnet("2001:db8::1") == "2001:db8::/64"
    assert FreeIPPool._public_subnet("not-an-ip") == ""


def test_select_public_rows_dedupes_and_caps_subnet():
    pool = object.__new__(FreeIPPool)
    rows = [
        {"url": "https://192.0.2.1:443", "egress_ip": "192.0.2.1"},
        {"url": "https://192.0.2.2:443", "egress_ip": "192.0.2.2"},
        {"url": "https://192.0.2.3:443", "egress_ip": "192.0.2.3"},
        {"url": "https://198.51.100.1:443", "egress_ip": "198.51.100.1"},
        {"url": "https://198.51.100.1:443", "egress_ip": "198.51.100.1"},
        {"url": "ftp://198.51.100.2:21", "egress_ip": "198.51.100.2"},
        {"url": "", "egress_ip": "203.0.113.1"},
    ]
    selected = pool._select_public_rows(
        rows,
        max_active=10,
        max_per_24=2,
    )
    assert [row["egress_ip"] for row in selected] == [
        "192.0.2.1",
        "192.0.2.2",
        "198.51.100.1",
    ]
    assert selected[0]["block"] == "192.0.2.0/24"


def test_select_public_rows_honours_max_active():
    pool = object.__new__(FreeIPPool)
    rows = [
        {"url": f"https://198.51.100.{index}:443", "egress_ip": f"198.51.100.{index}"}
        for index in range(1, 6)
    ]
    selected = pool._select_public_rows(rows, max_active=2, max_per_24=10)
    assert len(selected) == 2


def test_dedupe_by_ip_keeps_one_station_per_ip():
    pool = _dedupe_pool()
    stations = [
        SimpleNamespace(_station=2, current_ip="1.1.1.1", _egress_state="tunnel_up_full"),
        SimpleNamespace(_station=1, current_ip="1.1.1.1", _egress_state="tunnel_up_full"),
        SimpleNamespace(_station=3, current_ip="2.2.2.2", _egress_state="tunnel_up_full"),
        SimpleNamespace(_station=4, current_ip="", _egress_state="tunnel_up_full"),
    ]
    deduped = pool._dedupe_by_ip(stations)
    assert [station._station for station in deduped] == [1, 3, 4]


def test_dedupe_by_ip_prefers_full_egress_over_socks_down():
    pool = _dedupe_pool()
    stations = [
        SimpleNamespace(_station=1, current_ip="1.1.1.1", _egress_state="tunnel_up_socks_down"),
        SimpleNamespace(_station=2, current_ip="1.1.1.1", _egress_state="tunnel_up_full"),
    ]
    deduped = pool._dedupe_by_ip(stations)
    assert [station._station for station in deduped] == [2]


def _accounting_pool():
    pool = object.__new__(FreeIPPool)
    pool._stations = [
        SimpleNamespace(_station=1, requests=3),
        SimpleNamespace(_station=2, requests=4),
    ]
    pool._socks5_eps = [
        SimpleNamespace(provider="warp", enabled=True, usable=True, block="a", pid="warp-1", requests=5),
        SimpleNamespace(provider="warp", enabled=True, usable=False, block="a", pid="warp-2", requests=6),
        SimpleNamespace(provider="proxies", enabled=True, usable=True, block="b", pid="proxy-1", requests=7),
    ]
    pool._per_station = lambda item: {"request_count": int(getattr(item, "requests", 0))}
    pool._provider_matches = lambda item, name: str(getattr(item, "provider", "")) == name
    pool._socks5_client_usable = lambda item: bool(item.enabled and item.usable)
    return pool


def test_provider_and_endpoint_request_counts():
    pool = _accounting_pool()
    assert pool.provider_request_counts() == {
        "nordvpn": 7,
        "warp": 11,
        "proxies": 7,
        "direct": 0,
    }
    rows = {row["endpoint"]: row for row in pool.endpoint_request_counts()}
    assert rows["1"]["provider"] == "nordvpn"
    assert rows["1"]["requests"] == 3
    assert rows["warp-1"]["provider"] == "warp"
    assert rows["proxy-1"]["requests"] == 7


def test_egress_endpoint_counts_tracks_buckets_and_usability():
    pool = _accounting_pool()
    counts = pool.egress_endpoint_counts()
    assert counts["warp"] == {
        "configured": 2,
        "enabled": 2,
        "usable": 1,
        "unique_buckets": 1,
    }
    assert counts["proxies"] == {
        "configured": 1,
        "enabled": 1,
        "usable": 1,
        "unique_buckets": 1,
    }


def test_model_global_cooldown_remaining_and_clear(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("free.pool.time.monotonic", lambda: clock["now"])
    pool = object.__new__(FreeIPPool)
    pool._model_global_until = {"model": 130.0, "expired": 90.0}
    assert pool.model_global_remaining("model") == 30.0
    assert pool.model_global_remaining("expired") == 0.0
    assert "expired" not in pool._model_global_until
    assert pool.model_global_status() == [{"model": "model", "remaining_s": 30.0}]
    assert pool.clear_model_global("model") == 1
    assert pool.model_global_remaining("model") == 0.0


def _rotation_pool(*stations, repair_owner="manager", upstream_active=False):
    import asyncio
    import itertools

    pool = object.__new__(FreeIPPool)
    pool._stations = list(stations)
    pool._station_ids = {station._station for station in stations}
    pool._repair_owner = repair_owner
    pool._pending = set()
    pool._rotation_tasks = {}
    pool._rotation_seq = itertools.count()
    pool._rotation_queue = asyncio.PriorityQueue()
    pool._ensure_workers = lambda: None
    pool._upstream_event_active = lambda: upstream_active
    pool._upstream_event_remaining_s = lambda: 0.0
    pool.repairs = []
    pool._request_repair = lambda station, cause: pool.repairs.append((station, cause))
    return pool


def _station(sid: int):
    return SimpleNamespace(_station=sid, _geo_forced_pool=None)


def test_launch_rotation_delegates_to_supervisor():
    station = _station(1)
    pool = _rotation_pool(station, repair_owner="supervisor")
    pool._launch_rotation(station)
    assert pool.repairs == [(station, "rotation-delegated")]
    assert pool._pending == set()
    assert pool._rotation_queue.empty()


def test_launch_rotation_freezes_during_upstream_event():
    station = _station(1)
    pool = _rotation_pool(station, upstream_active=True)
    pool._launch_rotation(station)
    assert pool._pending == set()
    assert pool._rotation_queue.empty()


def test_launch_rotation_is_single_flight_per_station():
    station = _station(1)
    pool = _rotation_pool(station)
    pool._launch_rotation(station)
    pool._launch_rotation(station)
    assert pool._pending == {1}
    assert pool._rotation_queue.qsize() == 1
    queued = pool._rotation_queue.get_nowait()
    assert queued[2] is station


def test_launch_rotation_ignores_retired_station():
    pool = _rotation_pool()
    retired = _station(9)
    pool._launch_rotation(retired)
    assert pool._pending == set()
    assert pool._rotation_queue.empty()


def test_rotation_block_reason_precedence():
    from free.pool.rotation import (
        FROZEN,
        PENDING,
        RUNNING,
        UNKNOWN,
        rotation_block_reason,
    )

    common = {"station_id": 1, "station_ids": {1}, "pending": {1}}
    assert rotation_block_reason(
        upstream_active=True, task_running=True, **common
    ) == FROZEN
    assert rotation_block_reason(
        upstream_active=False,
        station_id=9,
        station_ids={1},
        pending=set(),
        task_running=False,
    ) == UNKNOWN
    assert rotation_block_reason(
        upstream_active=False, task_running=True, **common
    ) == PENDING
    assert rotation_block_reason(
        upstream_active=False,
        station_id=1,
        station_ids={1},
        pending=set(),
        task_running=True,
    ) == RUNNING
    assert rotation_block_reason(
        upstream_active=False,
        station_id=1,
        station_ids={1},
        pending=set(),
        task_running=False,
    ) is None


def test_ledger_ip_strips_logical_socks5_and_uses_confirmed_ip():
    from free.pool.quota import ledger_ip

    station = SimpleNamespace(current_ip="socks5:warp-a")
    assert ledger_ip(station) == ""
    assert (
        ledger_ip(
            station,
            per_station=lambda item: {"last_confirmed_ip": "203.0.113.9"},
        )
        == "203.0.113.9"
    )
    assert ledger_ip(SimpleNamespace(current_ip="198.51.100.1")) == "198.51.100.1"
    assert ledger_ip(SimpleNamespace(current_ip="198.51.100.1"), "socks5:x") == ""
    assert (
        ledger_ip(
            SimpleNamespace(current_ip="socks5:x"),
            per_station=lambda item: {"last_confirmed_ip": "socks5:y"},
        )
        == ""
    )


def test_selection_module_matches_pool_helpers():
    from free.pool.selection import dedupe_by_ip, egress_rank

    full = SimpleNamespace(_station=1, current_ip="1.1.1.1", _egress_state="tunnel_up_full")
    down = SimpleNamespace(_station=2, current_ip="1.1.1.1", _egress_state="tunnel_up_socks_down")
    assert egress_rank(full) == 0
    assert egress_rank(down) == 1
    ranked = dedupe_by_ip(
        [down, full],
        sort_key=lambda station: (egress_rank(station), station._station),
    )
    assert [station._station for station in ranked] == [1]


def test_public_pool_module_matches_pool_methods():
    from free.pool.public_pool import public_subnet, select_public_rows

    rows = [
        {"url": "https://192.0.2.1:443", "egress_ip": "192.0.2.1"},
        {"url": "https://192.0.2.2:443", "egress_ip": "192.0.2.2"},
        {"url": "https://192.0.2.1:443", "egress_ip": "192.0.2.1"},
    ]
    assert public_subnet("192.0.2.10") == FreeIPPool._public_subnet("192.0.2.10")
    assert select_public_rows(rows, max_active=5, max_per_24=1) == (
        FreeIPPool._select_public_rows(
            object.__new__(FreeIPPool),
            rows,
            max_active=5,
            max_per_24=1,
        )
    )


def test_clamp_seconds_bounds_and_invalid_values():
    from free.pool.config import clamp_seconds

    assert clamp_seconds({}, "k", 1.0, 10.0) is None
    assert clamp_seconds({"k": "bad"}, "k", 1.0, 10.0) is None
    assert clamp_seconds({"k": 5}, "k", 1.0, 10.0) == 5.0
    assert clamp_seconds({"k": 0.5}, "k", 1.0, 10.0) == 1.0
    assert clamp_seconds({"k": 20}, "k", 1.0, 10.0) == 10.0


def test_selection_helpers_are_pure_and_match_pool_contract():
    from free.pool.selection import (
        prefer_healthy,
        quality_stats,
        selection_latency,
        station_id,
    )

    healthy = SimpleNamespace(
        _station=1,
        _egress_state="tunnel_up_full",
        current_ip="1.1.1.1",
        _quota_per_ip=10,
        _server_load=2.0,
    )
    down = SimpleNamespace(
        _station=2,
        _egress_state="tunnel_up_socks_down",
        current_ip="2.2.2.2",
        _quota_per_ip=10,
        _server_load=None,
    )
    assert station_id(healthy) == 1
    assert station_id(SimpleNamespace()) == 0
    assert prefer_healthy(
        [down, healthy],
        degraded_rank=lambda station: 0,
    ) == [healthy]
    assert prefer_healthy(
        [down],
        degraded_rank=lambda station: 1,
    ) == [down]
    used, limit, load, inflight = quality_stats(
        healthy,
        per_station=lambda station: {"request_count": 3, "stream_tasks": [1, 2]},
    )
    assert (used, limit, load, inflight) == (3, 10, 2.0, 2.0)
    assert selection_latency(healthy, quality=None) == 2.0
    assert selection_latency(down, quality=None) is None


def test_update_config_refreshes_quota_profile():
    from free.ip_quota import IpQuotaRegistry

    pool = object.__new__(FreeIPPool)
    pool._quota = IpQuotaRegistry()
    pool._socks5_eps = []
    pool._per = {}
    pool._vpn = SimpleNamespace(_quota_per_ip=200)
    pool.update_config({"quota_per_ip": 200})
    assert pool._quota.profile.default_quota == 200
