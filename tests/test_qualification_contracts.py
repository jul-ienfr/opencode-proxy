"""Contract tests for the injectable qualification ports.

Each test drives the real consumer through a fake adapter with only the
documented protocol surface, so an accidental dependency on a concrete class is
caught before a production adapter is built.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from free.qualification import readmodel
from free.qualification.cutover import CutoverService, CutoverTarget
from free.qualification.discovery import DiscoveryConfig, DiscoveryService
from free.qualification.domain import Candidate
from free.qualification.source import CatalogStats
from free.qualification.station_adapter import RegistryStationAdapter
from free.qualification.store import QualificationStore
from free.qualification.watchdog import (
    StationObservation,
    WatchdogConfig,
    WatchdogService,
)


class FakeCatalogSource:
    source_id = "fake"
    path = "fake-api.json"

    def load(self):
        return [{"hostname": "a.nordvpn.com"}]

    def stats(self, rows):
        return CatalogStats(
            total=len(rows),
            online=len(rows),
            with_transports=len(rows),
            countries=1,
            source_id=self.source_id,
        )

    def candidates(self, rows, *, now=None):
        return [
            Candidate(
                hostname="a.nordvpn.com",
                country="Sweden",
                transports=("wireguard",),
                potential_score=70.0,
                first_seen_at=now or 100.0,
                last_seen_at=now or 100.0,
                updated_at=now or 100.0,
            )
        ]


class FakeBinder:
    def __init__(self):
        self.calls = []

    async def assign(self, station, hostname, country=None, **kwargs):
        self.calls.append((station, hostname, country, kwargs))
        return SimpleNamespace(ok=True, egress_ip="1.2.3.4")


class FakeProber:
    async def probe(self, station, model):
        return SimpleNamespace(status=200, valid=True, latency_ms=10.0)


class FakePorts:
    def __init__(self):
        self.calls = []

    async def prepare(self, target):
        self.calls.append("prepare")
        return True

    async def switch(self, target):
        self.calls.append("switch")
        return True

    async def probe(self, target):
        self.calls.append("probe")
        return True

    async def rollback(self, target):
        self.calls.append("rollback")
        return True


class FakeWatchdogProbe:
    async def probe(self, station):
        return StationObservation(
            station=station,
            connected=True,
            dns_ok=True,
            egress_ip="1.2.3.4",
            client_usable=True,
        )


class FakeRepair:
    def __init__(self):
        self.calls = []

    async def repair(self, station, cause):
        self.calls.append((station, cause))
        return True


def test_catalog_source_contract_is_sufficient_for_discovery(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    try:
        service = DiscoveryService(
            store,
            FakeCatalogSource(),
            config=DiscoveryConfig(source_id="fake"),
            clock=lambda: 100.0,
        )
        report = service.run(now=100.0, force=True)
        assert report.status == "ok"
        assert report.server_count == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_station_binder_and_prober_contract():
    binder = FakeBinder()
    adapter = RegistryStationAdapter(binder, FakeProber())
    candidate = Candidate(
        hostname="a.nordvpn.com",
        country="Sweden",
        transports=("openvpn-tcp",),
    )
    outcome = await adapter.qualify(candidate, "openvpn-tcp", 2)
    assert outcome.status == 200
    assert outcome.egress_ip == "1.2.3.4"
    station, hostname, country, kwargs = binder.calls[0]
    assert (station, hostname, country) == (2, "a.nordvpn.com", "Sweden")
    assert kwargs == {"vpn_type": "openvpn", "openvpn_protocol": "tcp"}


@pytest.mark.asyncio
async def test_cutover_ports_contract_order(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    ports = FakePorts()
    try:
        service = CutoverService(store, ports, clock=lambda: 100.0)
        result = await service.execute(
            CutoverTarget(station=1, to_option_key="a", to_egress_ip="1.2.3.4")
        )
        assert result.status == "committed"
        assert ports.calls == ["prepare", "switch", "probe"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_watchdog_probe_and_repair_contract(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    repair = FakeRepair()
    try:
        service = WatchdogService(
            store,
            FakeWatchdogProbe(),
            repair,
            config=WatchdogConfig(stations=(1,)),
            clock=lambda: 100.0,
        )
        report = await service.run_once(now=100.0)
        assert report.healthy == 1
        assert repair.calls == []
    finally:
        store.close()


def test_repair_queue_contract(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    try:
        job_id = store.record_repair_job(1, "tunnel_down", now=100.0)
        store.finish_repair_job(
            job_id,
            result="succeeded",
            last_error="",
            now=120.0,
        )
    finally:
        store.close()
    items = readmodel.repairs(tmp_path / "registry.db")["items"]
    assert items[0]["station"] == 1
    assert items[0]["result"] == "succeeded"
