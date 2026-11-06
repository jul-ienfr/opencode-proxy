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

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "",
        control_api=lambda station, timeout: "",
    )
    assert await egress(3) == ""
    assert egress.last_source == "none"


async def test_egress_falls_back_to_http_proxy_after_socks_failures():
    calls = []

    def get(url, proxies, timeout):
        calls.append(proxies["https"])
        if proxies["https"].startswith(("socks5h", "socks5")):
            raise RuntimeError("socks unavailable")
        return "186.247.44.63"

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "",
        control_api=lambda station, timeout: "",
    )
    assert await egress(2) == "186.247.44.63"
    assert calls[-1] == "http://127.0.0.1:8889"
    assert egress.last_source.startswith("http:")


async def test_egress_falls_back_to_gluetun_control_api():
    def get(url, proxies, timeout):
        raise RuntimeError("proxy path down")

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "",
        control_api=lambda station, timeout: "186.247.44.65",
    )
    assert await egress(5) == "186.247.44.65"
    assert egress.last_source.startswith("control_api:")


async def test_egress_falls_back_to_docker_exec():
    def get(url, proxies, timeout):
        raise RuntimeError("proxy path down")

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "186.247.44.64",
        control_api=lambda station, timeout: "",
    )
    assert await egress(4) == "186.247.44.64"
    assert egress.last_source.startswith("docker:")


async def test_egress_rejects_the_host_public_ip_at_every_layer():
    host = "37.69.230.71"

    def get(url, proxies, timeout):
        return host

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "186.247.44.66",
        control_api=lambda station, timeout: host,
        forbidden_ips={host},
    )
    assert await egress(2) == "186.247.44.66"
    assert egress.last_source.startswith("docker:")


async def test_egress_returns_empty_when_only_the_host_ip_is_available():
    host = "37.69.230.71"
    egress = StationEgress(
        get=lambda url, proxies, timeout: host,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: host,
        control_api=lambda station, timeout: host,
        forbidden_ips={host},
    )
    assert await egress(3) == ""
    assert egress.last_source == "none"


async def test_control_only_skips_the_proxy_paths():
    def get(url, proxies, timeout):
        raise AssertionError("proxy path must not run for control_only")

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "",
        control_api=lambda station, timeout: "186.247.44.67",
    )
    assert await egress.control_only(2) == "186.247.44.67"
    assert egress.last_source.startswith("control_api:")


async def test_control_only_rejects_the_host_ip():
    host = "37.69.230.71"
    egress = StationEgress(
        get=lambda url, proxies, timeout: "",
        urls=("https://one",),
        docker_exec=lambda station, url, timeout: "",
        control_api=lambda station, timeout: host,
        forbidden_ips={host},
    )
    assert await egress.control_only(2) == ""
    assert egress.last_source == "none"


def test_control_api_payloads_are_parsed():
    assert StationEgress._vpn_is_running('{"status":"running"}') is True
    assert StationEgress._vpn_is_running('{"status":"stopped"}') is False
    assert StationEgress._vpn_is_running("") is False
    assert StationEgress._public_ip('{"public_ip":"186.247.44.61"}') == (
        "186.247.44.61"
    )
    assert StationEgress._public_ip("186.247.44.62\n") == "186.247.44.62"
