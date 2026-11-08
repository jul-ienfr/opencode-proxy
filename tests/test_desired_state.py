"""Tests du desired state (011 T007)."""

from __future__ import annotations

import pytest

from ops.desired_state import DesiredEndpointSpec, DesiredStationSpec


def test_station_valid_and_hash_stable() -> None:
    spec = DesiredStationSpec(
        provider="nordvpn",
        station=1,
        stack="wireguard",
        transport="wireguard",
        container="opencode-vpn",
        port=1080,
    )
    assert spec.spec_hash == spec.spec_hash
    assert spec.bump(host="al41.nordvpn.com").generation == 2


def test_station_rejects_bad_provider_and_station() -> None:
    with pytest.raises(ValueError):
        DesiredStationSpec(provider="other", station=1)
    with pytest.raises(ValueError):
        DesiredStationSpec(provider="nordvpn", station=0)


def test_station_rejects_incoherent_stack() -> None:
    with pytest.raises(ValueError):
        DesiredStationSpec(
            provider="nordvpn",
            station=1,
            stack="socks5",
            transport="socks5",
        )


def test_endpoint_requires_loopback_for_warp() -> None:
    with pytest.raises(ValueError):
        DesiredEndpointSpec(
            provider="warp",
            label="warp-test",
            container="warp-test",
            port=11080,
            host="0.0.0.0",
        )


def test_endpoint_roundtrip() -> None:
    spec = DesiredEndpointSpec(
        provider="warp",
        label="warp-test",
        container="warp-test",
        port=11080,
        block="2a09:bac1:27a0:2060::/64",
    )
    restored = DesiredEndpointSpec.from_dict(spec.to_dict())
    assert restored == spec
    assert restored.spec_hash == spec.spec_hash
