"""Structured event logs for sweep, discovery and watchdog."""

from __future__ import annotations

import json
import logging

import pytest

from free.qualification.discovery import DiscoveryService
from free.qualification.domain import AttemptOutcome, Candidate
from free.qualification.source import NordVpnCatalogSource
from free.qualification.store import QualificationStore
from free.qualification.sweep import QualificationSweep, SweepConfig
from free.qualification.watchdog import (
    StationObservation,
    WatchdogConfig,
    WatchdogService,
)


class OkAdapter:
    async def qualify(self, candidate, transport, station):
        return AttemptOutcome(
            status=200,
            valid=True,
            egress_ip="1.2.3.4",
            rtt_ms=25.0,
        )


class HealthyProbe:
    async def probe(self, station):
        return StationObservation(
            station=station,
            connected=True,
            dns_ok=True,
            egress_ip="1.2.3.4",
            client_usable=True,
        )


def _candidate(hostname: str) -> Candidate:
    return Candidate(
        hostname=hostname,
        country="Sweden",
        transports=("wireguard",),
        potential_score=80.0,
        first_seen_at=100.0,
        last_seen_at=100.0,
        updated_at=100.0,
    )


def _records(caplog, event: str):
    return [record for record in caplog.records if record.getMessage() == event]


@pytest.mark.asyncio
async def test_sweep_logs_structured_attempts(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="free.qualification.sweep")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("se1.nordvpn.com")])
        sweep = QualificationSweep(
            store,
            OkAdapter(),
            config=SweepConfig(station=1, shadow=False),
            clock=lambda: 100.0,
        )
        await sweep.run_epoch(epoch_id="epoch-log", max_hosts=1)
    finally:
        store.close()
    attempts = _records(caplog, "qualification.attempt")
    assert attempts
    record = attempts[-1]
    assert record.hostname == "se1.nordvpn.com"
    assert record.transport == "wireguard"
    assert record.status == "200"
    assert record.epoch_id == "epoch-log"
    assert _records(caplog, "qualification.epoch.start")
    assert _records(caplog, "qualification.epoch.end")


def test_discovery_logs_success(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="free.qualification.discovery")
    cache = tmp_path / "api.json"
    cache.write_text(
        json.dumps(
            [
                {
                    "hostname": "se1.nordvpn.com",
                    "status": "online",
                    "load": 10,
                    "locations": [
                        {"country": {"name": "Sweden", "city": {"name": "Stockholm"}}}
                    ],
                    "technologies": [{"identifier": "wireguard_udp"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    store = QualificationStore(tmp_path / "registry.db")
    try:
        service = DiscoveryService(
            store,
            NordVpnCatalogSource(cache),
            clock=lambda: 100.0,
        )
        report = service.run(now=100.0, force=True)
        assert report.status == "ok"
    finally:
        store.close()
    records = _records(caplog, "qualification.discovery.ok")
    assert records
    assert records[-1].servers == 1
    assert records[-1].transports == 1


@pytest.mark.asyncio
async def test_watchdog_logs_run_summary(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="free.qualification.watchdog")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        service = WatchdogService(
            store,
            HealthyProbe(),
            config=WatchdogConfig(stations=(1,)),
            clock=lambda: 100.0,
        )
        report = await service.run_once(now=100.0)
        assert report.healthy == 1
    finally:
        store.close()
    records = _records(caplog, "qualification.watchdog.run")
    assert records
    assert records[-1].probed == 1
    assert records[-1].healthy == 1
    assert records[-1].errors == 0
