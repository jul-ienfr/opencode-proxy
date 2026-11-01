"""StationEgress tests (no network)."""

from __future__ import annotations

from free.ip_registry.adapters.egress import StationEgress


async def test_egress_returns_valid_ip():
    calls = []

    def get(url, proxies, timeout):
        calls.append((url, proxies))
        return "186.247.44.61\n"

    egress = StationEgress(get=get, urls=("https://one", "https://two"))
    ip = await egress(2)

    assert ip == "186.247.44.61"
    assert calls[0][1]["https"] == "socks5h://127.0.0.1:1081"
    assert "socks5h" in egress.last_source


async def test_egress_falls_back_to_local_dns_and_other_urls():
    calls = []

    def get(url, proxies, timeout):
        calls.append(proxies["https"])
        if len(calls) == 1:
            raise RuntimeError("remote dns")
        if len(calls) == 2:
            return "<html>not an ip</html>"
        return "186.247.44.62"

    egress = StationEgress(get=get, urls=("https://one", "https://two"))
    ip = await egress(1)

    assert ip == "186.247.44.62"
    assert calls[0] == "socks5h://127.0.0.1:1080"
    assert calls[-1] == "socks5://127.0.0.1:1080"


async def test_egress_returns_empty_when_all_fail():
    def get(url, proxies, timeout):
        return "not-an-ip"

    egress = StationEgress(get=get, urls=("https://one",))
    assert await egress(3) == ""
    assert egress.last_source == "none"
