"""test_station_count_config.py — N-station config resolution (plan 18/08 §4).

[plan 18/08 §4] The GUI dropdown 1-10 reads ``ip_rotation.station_count``;
the lifespan and the hot-reload POST resolve it through the single helper
``resolved_station_count`` (config/settings.py). Ports/container/service
names for station N are DERIVED defaults (socks5 1079+N / http 8887+N /
opencode-vpn-{N} / vpn-gluetun-{N}) with explicit ``_N`` keys taking
priority — station 1 and 2 must behave exactly as before.

Covered here (offline — no docker, no shared state):
  * station 3 derives 1082/8890 + opencode-vpn-3 + vpn-gluetun-3 +
    logs/vpn_state3.json; an explicit socks5_proxy_port_3 overrides.
  * station 1/2 defaults unchanged (the legacy values the whole system
    already runs on).
  * resolved_station_count: absent+dual_station → 2; absent without dual
    → 1; explicit station_count wins; clamped [1, 10]; non-int guard.
"""

import os

import vpn_manager as vm
from config.settings import resolved_station_count


def _mgr(station, **over):
    """Bare VPNManager — construction only, no docker/network side effects."""
    cfg = {"enabled": True, "proxy_mode": "vpn", "switch_delay": 0}
    cfg.update(over)
    return vm.VPNManager(cfg, station=station)


class TestPortDerivation:
    def test_station3_derived_defaults(self):
        m = _mgr(3)
        assert m._socks5_port == 1082  # 1079 + 3
        assert m._proxy_port == 8890  # 8887 + 3
        assert m._docker_container == "opencode-vpn-3"
        assert m._compose_service == "vpn-gluetun-3"
        assert m._state_file.endswith(os.path.join("logs", "vpn_state3.json"))

    def test_station1_legacy_defaults_unchanged(self):
        m = _mgr(1)
        assert m._socks5_port == 1080
        assert m._proxy_port == 8888
        assert m._docker_container == "opencode-vpn"
        assert m._compose_service == "vpn-gluetun"
        assert m._state_file.endswith(os.path.join("logs", "vpn_state.json"))

    def test_station2_defaults_unchanged(self):
        m = _mgr(2)
        assert m._socks5_port == 1081
        assert m._proxy_port == 8889
        assert m._docker_container == "opencode-vpn-2"
        assert m._compose_service == "vpn-gluetun-2"
        assert m._state_file.endswith(os.path.join("logs", "vpn_state2.json"))

    def test_explicit_keys_override_derived(self):
        m = _mgr(
            3,
            socks5_proxy_port_3=2099,
            vpn_proxy_port_3=9999,
            docker_container_3="custom-box",
            compose_service_3="my-gluetun-3",
            state_file_3="logs/custom3.json",
        )
        assert m._socks5_port == 2099
        assert m._proxy_port == 9999
        assert m._docker_container == "custom-box"
        assert m._compose_service == "my-gluetun-3"
        assert m._state_file.endswith("custom3.json")

    def test_station_defaults_are_isolated(self):
        """Station 3's derived ports must never leak into station 1."""
        m1, m3 = _mgr(1), _mgr(3)
        assert m1._socks5_port == 1080
        assert m1._proxy_port == 8888
        assert m3._socks5_port == 1082
        assert m3._proxy_port == 8890


class TestResolvedStationCount:
    def test_absent_without_dual_is_1(self):
        assert resolved_station_count({}) == 1

    def test_absent_with_dual_station_is_2(self):
        assert resolved_station_count({"dual_station": True}) == 2

    def test_explicit_wins_over_dual(self):
        assert resolved_station_count({"dual_station": True, "station_count": 4}) == 4

    def test_clamp_high(self):
        assert resolved_station_count({"station_count": 25}) == 10

    def test_clamp_low(self):
        assert resolved_station_count({"station_count": -3}) == 1
        assert resolved_station_count({"station_count": 0}) == 1

    def test_clamp_10_is_kept(self):
        assert resolved_station_count({"station_count": 10}) == 10

    def test_non_int_falls_back_to_dual(self):
        assert resolved_station_count({"station_count": "abc"}) == 1
        assert resolved_station_count({"dual_station": True, "station_count": "abc"}) == 2
