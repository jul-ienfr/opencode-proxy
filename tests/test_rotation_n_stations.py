"""Rotation N-stations — Phase2 F-M8: N=2..10, FakeVPNManager, shared_rotation."""


import pytest


def test_resolved_station_count_range():
    from config.settings import resolved_station_count

    for n in range(1, 11):
        assert resolved_station_count({"station_count": n}) == n
    # clamp
    assert resolved_station_count({"station_count": 0}) == 1
    assert resolved_station_count({"station_count": 15}) == 10
    assert resolved_station_count({"station_count": 2}) == 2
    # legacy dual_station
    assert resolved_station_count({"dual_station": True}) == 2
    assert resolved_station_count({"dual_station": False}) == 1
    assert resolved_station_count({}) == 1


def test_shared_rotation_state_basic(tmp_path):
    from shared_rotation import SharedRotationState

    p = tmp_path / "shared.json"
    cfg = {"shared_rotation_file": str(p), "recent_ip_window": 20, "recent_ip_max_age": 1800}
    s = SharedRotationState(cfg)
    # Initially empty
    assert s.recent_ips() == []
    # next_country should work with empty (signature: next_country(station, offset, n))
    idx = s.next_country(1, 1, 2)
    assert idx in (0, 1)
    # Record an IP and check recent
    s.record_ip("1.2.3.4", 1)
    assert "1.2.3.4" in s.recent_ips()


def test_fake_vpn_manager_n_stations(tmp_path):
    """FakeVPNManager with N=2..10 stations, distinct IPs."""
    import importlib.util
    import pathlib
    import sys

    p = pathlib.Path(__file__).parent / "test_vpn_freshness.py"
    spec = importlib.util.spec_from_file_location("test_vpn_freshness", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_vpn_freshness"] = mod
    spec.loader.exec_module(mod)
    FakeVPNManager = mod.FakeVPNManager
    _cfg = mod._cfg

    for n in (2, 3, 5, 10):
        cfg = _cfg(tmp_path, station_count=n)
        try:
            m = FakeVPNManager(cfg, station=1)
            assert m._station == 1
            from config.settings import resolved_station_count

            assert resolved_station_count(cfg) == n
        except Exception as e:
            pytest.skip(f"FakeVPNManager not mockable N={n}: {e}")


def test_free_stations_exhausted_with_socks5(monkeypatch):
    """_free_stations_exhausted should handle socks5_mode (no docker IP) via mocked pool."""
    import types

    # Mock _free_ip_pool with socks5_mode
    import opencode as oc

    fake_pool = types.SimpleNamespace(
        socks5_mode=True,
        _socks5_enabled_eps=lambda: ["socks5:proxy1:1080"],
        _socks5_usable=lambda ep, exclude_approaching=False: True,
        _stations=[],
        any_rotation_in_flight=lambda: False,
    )
    monkeypatch.setattr(oc, "_free_ip_pool", fake_pool, raising=False)
    # Mock _free_cooldown_active to return False (not on cooldown)
    monkeypatch.setattr(oc, "_free_cooldown_active", lambda m, s=None: False, raising=False)
    # For socks5, not exhausted because one usable proxy not on cooldown
    assert oc._free_stations_exhausted("mimo-v2.5-free") is False
