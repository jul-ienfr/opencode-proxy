"""P3 : unicité IP par station — un doublon est refusé au commit/adoption."""

from __future__ import annotations

from test_vpn_freshness import FakeVPNManager, _cfg

import shared_state
import vpn.manager as vm


def _pair(tmp_path):
    m1 = FakeVPNManager(_cfg(tmp_path), station=1, tmp_path=tmp_path)
    m2 = FakeVPNManager(_cfg(tmp_path), station=2, tmp_path=tmp_path)
    m1._status = vm.VPNState.CONNECTED
    m1._current_ip = "1.2.3.4"
    m2._status = vm.VPNState.CONNECTED
    m2._current_ip = None
    return m1, m2


def test_commit_refuses_ip_already_served_by_another_station(tmp_path):
    m1, m2 = _pair(tmp_path)
    old = getattr(shared_state, "vpn_managers", None)
    shared_state.vpn_managers = [m1, m2]
    try:
        assert m2._commit_ip("1.2.3.4") is False
        assert m2._current_ip is None
        assert "duplicate" in str(m2._last_rotation_error)
    finally:
        shared_state.vpn_managers = old


def test_commit_accepts_ip_when_peer_is_down(tmp_path):
    m1, m2 = _pair(tmp_path)
    m1._status = vm.VPNState.DISCONNECTED
    old = getattr(shared_state, "vpn_managers", None)
    shared_state.vpn_managers = [m1, m2]
    try:
        assert m2._commit_ip("1.2.3.4") is True
        assert m2._current_ip == "1.2.3.4"
    finally:
        shared_state.vpn_managers = old


def test_adopt_boot_ip_refuses_duplicate(tmp_path):
    m1, m2 = _pair(tmp_path)
    old = getattr(shared_state, "vpn_managers", None)
    shared_state.vpn_managers = [m1, m2]
    try:
        assert m2._adopt_boot_ip("1.2.3.4") is False
        assert m2._current_ip is None
    finally:
        shared_state.vpn_managers = old
