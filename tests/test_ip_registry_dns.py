"""DNS fallback policy and binder wiring."""

from __future__ import annotations

from pathlib import Path

from free.ip_registry.adapters.compose_binder import ComposeHostnameBinder
from free.ip_registry.adapters.hybrid_binder import HybridBinder
from free.ip_registry.dns import DnsFallback
from free.ip_registry.domain import BindResult

ROOT = Path(__file__).resolve().parent.parent


def test_dns_fallback_advances_after_threshold_and_resets():
    dns = DnsFallback(order=("dot", "plain"), failures_before_fallback=2)
    assert dns.mode(1) == "dot"
    assert dns.note_failure(1, "no-egress") == "dot"
    assert dns.note_failure(1, "no-egress") == "plain"
    assert dns.mode(1) == "plain"
    assert dns.mode(2) == "dot"
    dns.reset(1)
    assert dns.mode(1) == "dot"


def test_compose_override_carries_the_dns_mode(tmp_path):
    binder = ComposeHostnameBinder(tmp_path, settle_s=0)
    text = binder.override_yaml(2, "a.nordvpn.com", dns_mode="plain")
    assert "DNS_UPSTREAM_RESOLVER_TYPE=plain" in text
    # Gluetun refuses to boot with type=plain and no IPv4 upstream.
    assert "DNS_UPSTREAM_PLAIN_ADDRESSES=1.1.1.1:53,8.8.8.8:53,9.9.9.9:53" in text


class _Control:
    def __init__(self):
        self.calls = 0

    def current_transport(self):
        return "wireguard"

    async def assign(self, *args, **kwargs):
        self.calls += 1
        return BindResult(True, "a", egress_ip="1.1.1.1")

    async def release(self, station):
        return None

    async def current(self, station):
        return None


class _Compose:
    def __init__(self):
        self.calls = []

    async def assign(self, station, hostname, country=None, **kwargs):
        self.calls.append(kwargs)
        return BindResult(True, hostname, egress_ip="2.2.2.2")


async def test_hybrid_forces_compose_when_a_dns_mode_is_requested():
    control = _Control()
    compose = _Compose()
    binder = HybridBinder(control=control, compose=compose)
    result = await binder.assign(
        2, "a.nordvpn.com", vpn_type="wireguard", dns_mode="plain"
    )
    assert result.egress_ip == "2.2.2.2"
    assert control.calls == 0
    assert compose.calls[0]["dns_mode"] == "plain"
