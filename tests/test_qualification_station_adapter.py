"""Station adapter tests with fake binder/prober; no Docker/network."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from free.qualification.domain import Candidate
from free.qualification.station_adapter import (
    RegistryStationAdapter,
    build_registry_station_adapter,
    transport_parts,
)


@dataclass
class FakeBind:
    ok: bool
    hostname: str = ""
    egress_ip: str = ""
    error: str = ""


@dataclass
class FakeProbe:
    status: int
    latency_ms: float = 0.0
    retry_after_s: float = 0.0
    valid: bool = True
    validation: str = ""
    error: str = ""


class FakeBinder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def assign(self, station, hostname, country=None, **kwargs):
        self.calls.append((station, hostname, country, kwargs))
        return self.result


class FakeProber:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def probe(self, station, model):
        self.calls.append((station, model))
        return self.results.pop(0)


def _candidate() -> Candidate:
    return Candidate(hostname="se1.nordvpn.com", country="Sweden", transports=("wireguard",))


def test_transport_parts():
    assert transport_parts("wireguard") == ("wireguard", None)
    assert transport_parts("openvpn-udp") == ("openvpn", "udp")
    assert transport_parts("openvpn-tcp") == ("openvpn", "tcp")


@pytest.mark.asyncio
async def test_bind_failure_is_infra():
    adapter = RegistryStationAdapter(FakeBinder(FakeBind(False, error="control-no-egress")), FakeProber([]))
    outcome = await adapter.qualify(_candidate(), "wireguard", 1)
    assert outcome.status == 0
    assert outcome.error.startswith("bind:")


@pytest.mark.asyncio
async def test_200_returns_egress_and_latency():
    adapter = RegistryStationAdapter(
        FakeBinder(FakeBind(True, egress_ip="1.2.3.4")),
        FakeProber([FakeProbe(200, latency_ms=80.0)]),
    )
    outcome = await adapter.qualify(_candidate(), "openvpn-tcp", 2)
    assert outcome.status == 200
    assert outcome.egress_ip == "1.2.3.4"
    assert outcome.latency_ms == 80.0


@pytest.mark.asyncio
async def test_429_returns_quota():
    adapter = RegistryStationAdapter(
        FakeBinder(FakeBind(True, egress_ip="1.2.3.4")),
        FakeProber([FakeProbe(429, retry_after_s=3600.0)]),
    )
    outcome = await adapter.qualify(_candidate(), "wireguard", 1)
    assert outcome.status == 429
    assert outcome.retry_after_s == 3600.0


@pytest.mark.asyncio
async def test_model_fallback_on_transport_error():
    adapter = RegistryStationAdapter(
        FakeBinder(FakeBind(True, egress_ip="1.2.3.4")),
        FakeProber([FakeProbe(0, error="timeout"), FakeProbe(200, latency_ms=50.0)]),
        model="first",
        models=("first", "second"),
    )
    outcome = await adapter.qualify(_candidate(), "wireguard", 1)
    assert outcome.status == 200


def test_build_adapter_from_config(tmp_path):
    config = {
        "ip_rotation": {"egress_wait_s": 5},
        "ip_registry": {
            "allocator": {
                "probe_models": ["first", "second"],
                "probe_timeout_s": 10,
                "settle_s": 1,
                "egress_wait_s": 5,
            }
        },
    }
    adapter = build_registry_station_adapter(config, root=tmp_path, station=1)
    assert adapter.model == "first"
    assert adapter.models == ("first", "second")
    assert adapter.binder is not None
    assert adapter.prober is not None

