"""Characterization tests for the VPN manager's pure control pieces."""

from __future__ import annotations

from types import SimpleNamespace

import shared_state
import vpn.manager as manager_module
from vpn.manager import BackoffTimer, CircuitBreaker, VPNManager, VPNState


def test_vpn_state_transition_matrix():
    assert VPNState.can_transition(VPNState.DISCONNECTED, VPNState.CONNECTING)
    assert VPNState.can_transition(VPNState.CONNECTING, VPNState.CONNECTED)
    assert VPNState.can_transition(VPNState.CONNECTED, VPNState.DEGRADED)
    assert VPNState.can_transition(VPNState.DEGRADED, VPNState.CONNECTED)
    assert VPNState.can_transition(VPNState.ERROR, VPNState.CONNECTING)
    assert VPNState.can_transition(VPNState.DISCONNECTING, VPNState.DISCONNECTED)
    assert not VPNState.can_transition(VPNState.DISCONNECTED, VPNState.CONNECTED)
    assert not VPNState.can_transition(VPNState.DISCONNECTING, VPNState.CONNECTING)
    assert not VPNState.can_transition("unknown", VPNState.CONNECTED)


def test_vpn_state_is_up_and_routable():
    assert VPNState.is_up(VPNState.CONNECTED)
    assert VPNState.is_up(VPNState.DEGRADED)
    assert VPNState.is_routable(VPNState.DEGRADED)
    assert not VPNState.is_up(VPNState.CONNECTING)
    assert not VPNState.is_routable(VPNState.ERROR)


def test_backoff_sequence_caps_and_resets():
    timer = BackoffTimer(base_delay=5.0, max_delay=20.0, multiplier=2.0)
    assert timer.delay == 5.0
    timer.record_failure()
    assert timer.delay == 5.0
    timer.record_failure()
    assert timer.delay == 10.0
    timer.record_failure()
    assert timer.delay == 20.0
    timer.record_failure()
    assert timer.delay == 20.0
    assert timer.consecutive_failures == 4
    timer.record_success()
    assert timer.delay == 5.0
    assert timer.consecutive_failures == 0


def test_circuit_breaker_open_half_open_and_single_probe(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "vpn.manager.time.monotonic",
        lambda: clock["now"],
    )
    breaker = CircuitBreaker(failure_threshold=3, recovery_time=10.0)
    assert breaker.is_available("a") is True
    for _ in range(3):
        breaker.record_failure("a")
    assert breaker.get_status()["a"] == {"failures": 3, "state": "open"}
    assert breaker.is_available("a") is False
    clock["now"] = 10.0
    assert breaker.is_available("a") is True
    assert breaker.is_available("a") is False
    breaker.record_failure("a")
    assert breaker.get_status()["a"]["state"] == "open"
    clock["now"] = 20.0
    assert breaker.is_available("a") is True
    breaker.record_success("a")
    assert breaker.get_status()["a"] == {"failures": 0, "state": "closed"}
    assert breaker.is_available("a") is True


def test_circuit_breaker_unknown_server_is_available():
    breaker = CircuitBreaker()
    assert breaker.is_available("never-seen") is True
    assert breaker.get_status() == {}


def test_station_bind_keys_are_per_station(tmp_path):
    station1 = VPNManager(
        {"enabled": False, "state_file": str(tmp_path / "s1.json")},
        1,
    )
    station2 = VPNManager(
        {
            "enabled": False,
            "state_file_2": str(tmp_path / "s2.json"),
        },
        2,
    )
    assert (
        station1._docker_container,
        station1._compose_service,
        station1._proxy_port,
        station1._socks5_port,
    ) == ("opencode-vpn", "vpn-gluetun", 8888, 1080)
    assert (
        station2._docker_container,
        station2._compose_service,
        station2._proxy_port,
        station2._socks5_port,
    ) == ("opencode-vpn-2", "vpn-gluetun-2", 8889, 1081)


def _bare_manager():
    manager = object.__new__(VPNManager)
    manager._station = 1
    manager._status = VPNState.DISCONNECTED
    manager._current_ip = None
    manager._current_server = None
    manager._connected_at = None
    manager._connect_started_at = None
    manager._degraded_hold_until = 0.0
    manager._health_policy = None
    manager._publish_vpn_event = lambda: None
    return manager


def test_set_status_transitions_and_event(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock["now"])
    manager = _bare_manager()
    events = []
    manager._publish_vpn_event = lambda: events.append(manager._status)
    manager._set_status(VPNState.CONNECTING)
    assert manager._status == VPNState.CONNECTING
    assert manager._connect_started_at == 100.0
    assert events == [VPNState.CONNECTING]
    manager._set_status(VPNState.CONNECTED)
    assert manager._connect_started_at is None
    assert manager._connected_at == 100.0
    assert events[-1] == VPNState.CONNECTED
    manager._set_status(VPNState.CONNECTED)
    assert len(events) == 2


def test_set_status_degraded_hold_and_force(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock["now"])
    manager = _bare_manager()
    manager._health_policy = SimpleNamespace(degraded_hold_s=5.0)
    manager._set_status(VPNState.CONNECTED)
    manager._set_status(VPNState.DEGRADED)
    assert manager._degraded_hold_until == 5.0
    clock["now"] = 1.0
    manager._set_status(VPNState.CONNECTED)
    assert manager._status == VPNState.DEGRADED
    clock["now"] = 6.0
    manager._set_status(VPNState.CONNECTED)
    assert manager._status == VPNState.CONNECTED


def test_valid_ip_rejects_html_and_accepts_v4_v6():
    assert VPNManager._valid_ip("1.2.3.4") == "1.2.3.4"
    assert VPNManager._valid_ip("2001:db8::1") == "2001:db8::1"
    assert VPNManager._valid_ip("<html>error</html>") is None
    assert VPNManager._valid_ip("") is None
    assert VPNManager._valid_ip("1" * 65) is None
    assert VPNManager._valid_ip(None) is None


def test_auth_support_and_cooling(monkeypatch):
    with_text = _bare_manager()
    with_text._check_auth_failed = lambda text=None: None
    assert with_text._auth_check_supports_text() is True
    with_text._check_auth_failed = lambda: None
    assert with_text._auth_check_supports_text() is True

    without_text = _bare_manager()
    without_text._check_auth_failed = lambda: None
    assert without_text._auth_check_supports_text() is False

    clock = {"now": 50.0}
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock["now"])
    auth = _bare_manager()
    auth._now_fn = lambda: clock["now"]
    auth._auth_cool_until = 100.0
    assert auth._auth_cooling() is True
    assert auth._auth_cool_remaining_s() == 50
    clock["now"] = 150.0
    assert auth._auth_cooling() is False
    assert auth._auth_cool_remaining_s() == 0


def test_needs_egress_check_flags_and_auth_cooling():
    manager = _bare_manager()
    manager._now_fn = lambda: 10.0
    manager._auth_cool_until = 0.0
    assert manager._needs_egress_check() is False
    for attribute in (
        "_egress_socks_pending",
        "_server_issue_pending",
        "_auth_repin_needed",
    ):
        setattr(manager, attribute, True)
        assert manager._needs_egress_check() is True
        setattr(manager, attribute, False)
    manager._egress_failures = 1
    assert manager._needs_egress_check() is True
    manager._egress_failures = 0
    manager._auth_cool_until = 20.0
    assert manager._needs_egress_check() is True


def test_recent_ip_shared_and_local_fallback():
    class Shared:
        def __init__(self, *, fail=False):
            self.recorded = []
            self.fail = fail

        def is_recent(self, ip):
            if self.fail:
                raise RuntimeError("registry down")
            return ip == "9.9.9.9"

        def record_ip(self, ip, station):
            self.recorded.append((ip, station))

    manager = _bare_manager()
    shared = Shared()
    manager._shared = shared
    manager._ip_history = [{"ip": "1.1.1.1"}]
    assert manager._ip_recent("9.9.9.9") is True
    assert manager._ip_recent("1.1.1.1") is True
    assert manager._ip_recent("2.2.2.2") is False
    manager._shared = Shared(fail=True)
    assert manager._ip_recent("1.1.1.1") is True
    manager._record_ip_change("3.3.3.3")
    assert manager._shared.recorded == [("3.3.3.3", 1)]


def test_ip_in_use_by_other_skips_self_and_down(monkeypatch):
    manager = _bare_manager()
    peer = SimpleNamespace(
        status=VPNState.CONNECTED,
        current_ip="4.4.4.4",
        _station=3,
    )
    down = SimpleNamespace(
        status=VPNState.ERROR,
        current_ip="5.5.5.5",
        _station=4,
    )
    monkeypatch.setattr(
        shared_state,
        "vpn_managers",
        [manager, peer, down],
        raising=False,
    )
    assert manager._ip_in_use_by_other("4.4.4.4") == 3
    assert manager._ip_in_use_by_other("5.5.5.5") is None
    assert manager._ip_in_use_by_other("") is None


def test_advance_identity_local_and_shared():
    manager = _bare_manager()
    manager._identity_rotation_enabled = False
    manager._identity_index = 2
    manager._identity_profiles = ["a", "b", "c"]
    manager._shared = None
    assert manager._advance_identity() == 2

    manager._identity_rotation_enabled = True
    manager._proxy_mode = "vpn"
    manager._identity_profiles = ["a"]
    assert manager._advance_identity() == 0

    manager._identity_profiles = ["a", "b", "c"]
    manager._identity_index = 2
    assert manager._advance_identity() == 0

    class Shared:
        def next_identity(self, station, count):
            assert (station, count) == (1, 3)
            return 1

    manager._shared = Shared()
    assert manager._advance_identity() == 1

    class Broken:
        def next_identity(self, station, count):
            raise RuntimeError("down")

    manager._shared = Broken()
    manager._identity_index = 1
    assert manager._advance_identity() == 2


def test_feed_pin_windows_prunes_older_than_one_hour():
    manager = _bare_manager()
    manager._now_fn = lambda: 4000.0
    manager._pin_resets_1h = [100.0, 300.0, 3900.0]
    manager._pin_auths_1h = [200.0]
    manager._feed_pin_windows(auth_live=True, issue_live=True)
    assert manager._pin_resets_1h == [3900.0, 4000.0]
    assert manager._pin_auths_1h == [4000.0]
    manager._pin_resets_1h = []
    manager._pin_auths_1h = []
    manager._feed_pin_windows(auth_live=False, issue_live=False)
    assert manager._pin_resets_1h == []
