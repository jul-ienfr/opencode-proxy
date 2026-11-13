"""Egress adapter tests: the fork proof first, the five cold layers after it."""

from __future__ import annotations

from types import SimpleNamespace

from free.ip_registry.adapters.egress import StationEgress


def verified(ip: str) -> SimpleNamespace:
    return SimpleNamespace(verified=True, ip=ip)


def unverified(reason: str = "no_quorum") -> SimpleNamespace:
    return SimpleNamespace(verified=False, ip="", reason=reason)


async def test_control_plane_proof_wins_and_skips_every_cold_layer():
    def get(_url, _proxies, _timeout):
        raise AssertionError("cold proxy layer must not run when the proof is verified")

    calls: list[int] = []

    def control_plane(station: int):
        calls.append(station)
        return verified("203.0.113.50")

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda *_: raise_assertion("docker must not run"),
        control_api=lambda *_: raise_assertion("control_api must not run"),
        control_plane=control_plane,
    )

    assert await egress(2) == "203.0.113.50"
    assert calls == [2]
    assert egress.last_source == "control_plane:/v1/egress"


async def test_unverified_proof_falls_back_to_the_same_cold_layers():
    def get(_url, _proxies, _timeout):
        return "203.0.113.51"

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda *_: "",
        control_api=lambda *_: "",
        control_plane=lambda _station: unverified(),
    )

    assert await egress(2) == "203.0.113.51"
    assert egress.last_source.startswith("socks5h:")


async def test_control_plane_error_falls_back_without_raising():
    egress = StationEgress(
        get=lambda *_: "203.0.113.52",
        urls=("https://one",),
        docker_exec=lambda *_: "",
        control_api=lambda *_: "",
        control_plane=lambda _station: (_ for _ in ()).throw(RuntimeError("fork down")),
    )

    assert await egress(2) == "203.0.113.52"
    assert egress.last_source.startswith("socks5h:")


async def test_forbidden_host_ip_is_rejected_before_acceptance():
    host = "37.69.230.71"
    egress = StationEgress(
        get=lambda *_: host,
        urls=("https://one",),
        docker_exec=lambda *_: "",
        control_api=lambda *_: "",
        control_plane=lambda _station: verified(host),
        forbidden_ips={host},
    )

    assert await egress(2) == ""
    assert egress.last_source == "none"


async def test_control_only_uses_the_control_plane_proof_first():
    def get(_url, _proxies, _timeout):
        raise AssertionError("control_only must not use proxies")

    egress = StationEgress(
        get=get,
        urls=("https://one",),
        docker_exec=lambda *_: "",
        control_api=lambda *_: raise_assertion("the cold control API must not run"),
        control_plane=lambda station: verified("203.0.113.53") if station == 4 else unverified(),
    )

    assert await egress.control_only(4) == "203.0.113.53"
    assert egress.last_source == "control_plane:/v1/egress"


async def test_control_only_falls_back_to_the_control_api_when_unverified():
    egress = StationEgress(
        get=lambda *_: "",
        urls=("https://one",),
        docker_exec=lambda *_: "",
        control_api=lambda *_: "203.0.113.54",
        control_plane=lambda _station: unverified(),
    )

    assert await egress.control_only(2) == "203.0.113.54"
    assert egress.last_source.startswith("control_api:")


def raise_assertion(message: str):
    raise AssertionError(message)
