"""Regression: fresh_chain must only ever remove ITS station's container.

The 2026-11-01 incident: station 1's chain matched containers with
``service in name`` where service was ``opencode-vpn`` — every
``opencode-vpn-2`` … ``-6`` matched, so each chain iteration deleted the
whole serving fleet. The scope is exact-name (or compose hashed-prefix)
only.
"""

from scripts.fresh_chain import station_container_names


def test_station1_never_matches_other_stations():
    names = [
        "opencode-vpn",
        "opencode-vpn-2",
        "opencode-vpn-3",
        "opencode-vpn-4",
        "opencode-vpn-5",
        "opencode-vpn-6",
        "opencode-vpn-10",
        "opencode-proxy",
        "vpn-gluetun-6",
        "abc123_opencode-vpn",
    ]
    assert station_container_names(names, 1) == ["opencode-vpn", "abc123_opencode-vpn"]


def test_station5_only_matches_itself():
    names = [
        "opencode-vpn",
        "opencode-vpn-5",
        "opencode-vpn-50",
        "opencode-vpn-6",
        "def456_opencode-vpn-5",
    ]
    assert station_container_names(names, 5) == ["opencode-vpn-5", "def456_opencode-vpn-5"]


def test_station_number_is_not_a_prefix_match():
    names = ["opencode-vpn-1", "opencode-vpn-10", "opencode-vpn-11"]
    assert station_container_names(names, 1) == []
