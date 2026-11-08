"""Tests des adapters NordVPN/WARP (011 T101/T102)."""

from __future__ import annotations

from types import SimpleNamespace

from ops.providers.nordvpn import NordVpnProvider
from ops.providers.warp import WarpProvider


class _Manager:
    _station = 1
    _docker_container = "opencode-vpn"

    def get_status(self):
        return {
            "status": "connected",
            "ip": "87.120.102.85",
            "assigned_stack": "wireguard",
            "ovpn_protocol_effective": "wireguard",
            "pinned_hostname": "al41.nordvpn.com",
            "assigned_country": "Albania",
            "proxy_port": 1080,
            "container": "opencode-vpn",
        }


def test_nordvpn_specs_and_observe() -> None:
    provider = NordVpnProvider([_Manager()])
    specs = provider.specs()
    assert len(specs) == 1
    assert specs[0].provider == "nordvpn"
    assert specs[0].station == 1
    assert specs[0].host == "al41.nordvpn.com"
    observation = provider.observe(specs[0])
    assert observation.client_usable is True
    assert observation.egress_ip == "87.120.102.85"


def _endpoint(**kwargs):
    defaults = {
        "label": "warp-test",
        "container": "warp-test",
        "port": 11080,
        "host": "127.0.0.1",
        "ipv6": True,
        "managed": True,
        "enabled": True,
        "image": "caomingjun/warp:latest",
        "egress_ip": "2a09:bac1:27a0:2060::1",
        "block": "2a09:bac1:27a0:2060::/64",
        "_station": -2,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class _Provisioner:
    def __init__(self):
        self.calls = []

    def apply(self, specs, actual=None):
        self.calls.append((specs, actual))
        return {"ok": True}

    def delete(self, container, confirm=False):
        self.calls.append((container, confirm))
        return {"ok": True}


def test_warp_specs_and_observe() -> None:
    provider = WarpProvider([_endpoint()])
    specs = provider.specs()
    assert len(specs) == 1
    assert specs[0].label == "warp-test"
    assert specs[0].host == "127.0.0.1"
    observation = provider.observe(specs[0])
    assert observation.client_usable is True
    assert observation.block == "2a09:bac1:27a0:2060::/64"


def test_warp_create_uses_provisioner() -> None:
    provisioner = _Provisioner()
    provider = WarpProvider([_endpoint()], provisioner=provisioner)
    spec = provider.specs()[0]
    provider.create(spec)
    assert provisioner.calls
    assert provisioner.calls[0][1] == {}


def test_warp_delete_requires_confirm_and_uses_provisioner() -> None:
    provisioner = _Provisioner()
    provider = WarpProvider([_endpoint()], provisioner=provisioner)
    spec = provider.specs()[0]
    provider.delete(spec)
    assert provisioner.calls == [("warp-test", True)]
