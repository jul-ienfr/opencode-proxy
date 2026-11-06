"""PreflightProber tests (no network, no Docker)."""

from __future__ import annotations

import asyncio

from free.ip_registry.preflight import (
    PreflightProber,
    PreflightResult,
    PreflightSummary,
)


async def _ok_resolve(host, port):
    return ["1.2.3.4"]


async def test_probe_success_collects_dns_endpoint_and_handshake():
    async def tcp(ip, port, timeout):
        assert ip == "1.2.3.4"
        assert port == 443
        return 12.0

    async def handshake(ip, transport, timeout):
        return 3.0

    prober = PreflightProber(
        resolve=_ok_resolve,
        tcp_connect=tcp,
        handshake=handshake,
    )
    result = await prober.probe("nl1.nordvpn.com", "openvpn-tcp")

    assert result.ok
    assert result.endpoint_ip == "1.2.3.4"
    assert result.rtt_ms == 15.0
    assert result.error == ""


async def test_probe_reports_dns_failure():
    async def resolve(host, port):
        raise OSError("dns down")

    prober = PreflightProber(resolve=resolve)
    result = await prober.probe("nl1.nordvpn.com", "openvpn-tcp")

    assert not result.ok
    assert result.error.startswith("dns:")
    assert not result.dns_ok


async def test_probe_reports_endpoint_failure():
    async def tcp(ip, port, timeout):
        raise TimeoutError("tcp")

    prober = PreflightProber(resolve=_ok_resolve, tcp_connect=tcp)
    result = await prober.probe("nl1.nordvpn.com", "openvpn-tcp")

    assert not result.ok
    assert result.dns_ok
    assert result.error.startswith("endpoint:")


async def test_probe_reports_handshake_failure():
    async def udp(ip, port, timeout):
        return 4.0

    async def handshake(ip, transport, timeout):
        raise TimeoutError("no handshake")

    prober = PreflightProber(
        resolve=_ok_resolve,
        udp_probe=udp,
        handshake=handshake,
    )
    result = await prober.probe("wg1.nordvpn.com", "wireguard")

    assert not result.ok
    assert result.endpoint_ok
    assert result.error.startswith("handshake:")


async def test_empty_hostname_and_unknown_transport_are_explicit():
    prober = PreflightProber(resolve=_ok_resolve)
    empty = await prober.probe("", "openvpn-tcp")
    unknown = await prober.probe("a.nordvpn.com", "bogus")
    assert empty.error == "empty-hostname"
    assert unknown.error.startswith("unknown-transport:")


async def test_preflight_many_respects_concurrency_and_returns_every_item():
    state = {"active": 0, "max": 0}

    async def tcp(ip, port, timeout):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        try:
            await asyncio.sleep(0.01)
            return 1.0
        finally:
            state["active"] -= 1

    prober = PreflightProber(resolve=_ok_resolve, tcp_connect=tcp)
    candidates = [
        {"host": f"h{i}.nordvpn.com", "country": "Germany"} for i in range(100)
    ]
    results = await prober.preflight_many(
        candidates, "openvpn-tcp", concurrency=8, timeout=1, budget_s=5
    )

    assert len(results) == 100
    assert all(result.ok for result in results)
    assert state["max"] <= 8
    summary = PreflightSummary.from_results(results)
    assert summary.total == 100
    assert summary.ok == 100
    assert summary.failed == 0
    assert summary.rtt_p50_ms == 1.0


async def test_preflight_many_budget_marks_slow_items_without_stalling():
    async def tcp(ip, port, timeout):
        await asyncio.sleep(1.0)
        return 1.0

    prober = PreflightProber(resolve=_ok_resolve, tcp_connect=tcp)
    results = await prober.preflight_many(
        [{"host": "slow.nordvpn.com"}],
        "openvpn-tcp",
        concurrency=1,
        timeout=5,
        budget_s=0.01,
    )

    assert len(results) == 1
    assert results[0].error == "budget"


def test_summary_counts_error_families():
    summary = PreflightSummary.from_results(
        [
            PreflightResult("a", "openvpn-tcp", dns_ok=True, endpoint_ok=True, handshake_ok=True, rtt_ms=10),
            PreflightResult("b", "openvpn-tcp", error="dns:timeout"),
            PreflightResult("c", "openvpn-tcp", error="endpoint:timeout"),
            PreflightResult("d", "openvpn-tcp", error="endpoint:timeout"),
        ]
    )
    assert summary.total == 4
    assert summary.ok == 1
    assert summary.failed == 3
    assert summary.errors == {"dns": 1, "endpoint": 2}
