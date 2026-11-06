"""HybridBinder routing tests (no Docker, no network)."""

from __future__ import annotations

from free.ip_registry.adapters.hybrid_binder import HybridBinder, transport_name
from free.ip_registry.domain import BindResult


class FakeControl:
    def __init__(self, transport: str, result: BindResult) -> None:
        self.transport = transport
        self.result = result
        self.calls: list[tuple] = []

    def current_transport(self) -> str:
        return self.transport

    async def assign(self, station, hostname, country=None, **kwargs):
        self.calls.append((station, hostname, country, kwargs))
        return self.result

    async def release(self, station):
        return None

    async def current(self, station):
        return "186.247.44.61"


class FakeCompose:
    def __init__(self, result: BindResult) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def assign(self, station, hostname, country=None, **kwargs):
        self.calls.append((station, hostname, country, kwargs))
        return self.result


def test_transport_name_covers_the_three_stacks():
    assert transport_name("wireguard") == "wireguard"
    assert transport_name("openvpn", "udp") == "openvpn-udp"
    assert transport_name("openvpn", "tcp") == "openvpn-tcp"
    assert transport_name(None) == "wireguard"


async def test_same_transport_uses_the_control_api():
    control = FakeControl("wireguard", BindResult(True, "a", egress_ip="1.1.1.1"))
    compose = FakeCompose(BindResult(True, "a", egress_ip="2.2.2.2"))
    binder = HybridBinder(control=control, compose=compose)

    result = await binder.assign(2, "a.nordvpn.com", vpn_type="wireguard")

    assert result.egress_ip == "1.1.1.1"
    assert len(control.calls) == 1
    assert compose.calls == []


async def test_control_no_egress_is_not_hidden_by_a_recreate():
    control = FakeControl(
        "wireguard", BindResult(False, "a", error="control-no-egress")
    )
    compose = FakeCompose(BindResult(True, "a", egress_ip="2.2.2.2"))
    binder = HybridBinder(control=control, compose=compose)

    result = await binder.assign(2, "a.nordvpn.com", vpn_type="wireguard")

    assert not result.ok
    assert result.error == "control-no-egress"
    assert compose.calls == []


async def test_control_unavailable_falls_back_to_compose():
    control = FakeControl(
        "wireguard", BindResult(False, "a", error="control-unavailable:put")
    )
    compose = FakeCompose(BindResult(True, "a", egress_ip="2.2.2.2"))
    binder = HybridBinder(control=control, compose=compose)

    result = await binder.assign(2, "a.nordvpn.com", vpn_type="wireguard")

    assert result.ok
    assert result.egress_ip == "2.2.2.2"
    assert len(compose.calls) == 1


async def test_transport_change_goes_straight_to_compose():
    control = FakeControl("wireguard", BindResult(True, "a", egress_ip="1.1.1.1"))
    compose = FakeCompose(BindResult(True, "a", egress_ip="2.2.2.2"))
    binder = HybridBinder(control=control, compose=compose)

    result = await binder.assign(
        2, "a.nordvpn.com", vpn_type="openvpn", openvpn_protocol="tcp"
    )

    assert result.egress_ip == "2.2.2.2"
    assert control.calls == []
    assert len(compose.calls) == 1


async def test_current_transport_is_exposed_to_the_chain():
    control = FakeControl("openvpn-tcp", BindResult(True, "a"))
    compose = FakeCompose(BindResult(True, "a"))
    binder = HybridBinder(control=control, compose=compose)
    assert binder.current_transport(2) == "openvpn-tcp"
    assert binder.current_transport(3) == "openvpn-tcp"


async def test_compose_success_updates_the_transport_cache():
    control = FakeControl("wireguard", BindResult(True, "a", egress_ip="1.1.1.1"))
    compose = FakeCompose(BindResult(True, "a", egress_ip="2.2.2.2"))
    binder = HybridBinder(control=control, compose=compose)

    first = await binder.assign(
        2, "a.nordvpn.com", vpn_type="openvpn", openvpn_protocol="udp"
    )
    second = await binder.assign(
        2, "b.nordvpn.com", vpn_type="openvpn", openvpn_protocol="udp"
    )

    assert first.ok and second.ok
    assert len(compose.calls) == 1
    assert len(control.calls) == 1
