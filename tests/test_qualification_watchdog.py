"""Watchdog tests: local repair, bounded escalation, healthy isolation."""

from __future__ import annotations

import pytest

from free.qualification.store import QualificationStore
from free.qualification.watchdog import (
    StationObservation,
    WatchdogConfig,
    WatchdogService,
    classify_observation,
)


class FakeProbe:
    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    async def probe(self, station):
        self.calls.append(station)
        value = self.observations[station]
        if isinstance(value, Exception):
            raise value
        return value


class FakeRepair:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def repair(self, station, cause):
        self.calls.append((station, cause))
        return self.result


def healthy(station: int = 1) -> StationObservation:
    return StationObservation(
        station=station,
        connected=True,
        dns_ok=True,
        egress_ip="1.2.3.4",
        client_usable=True,
        country="Sweden",
        hostname="se1.nordvpn.com",
        transport="wireguard",
    )


def tunnel_down(station: int = 2) -> StationObservation:
    return StationObservation(station=station, connected=False, error="tunnel down")


def test_classify_observation_order():
    assert classify_observation(healthy()) == "healthy"
    assert classify_observation(tunnel_down()) == "tunnel_down"
    assert classify_observation(StationObservation(station=1, connected=True, dns_ok=False)) == "dns_broken"
    assert classify_observation(StationObservation(station=1, connected=True, dns_ok=True)) == "no_egress"
    assert (
        classify_observation(StationObservation(station=1, connected=True, dns_ok=True, egress_ip="1.1.1.1"))
        == "client_unusable"
    )
    assert classify_observation(StationObservation(station=1, auth_failed=True)) == "auth_failed"


@pytest.mark.asyncio
async def test_local_repair_does_not_touch_healthy_station(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        probe = FakeProbe({1: healthy(1), 2: tunnel_down(2)})
        repair = FakeRepair(True)
        service = WatchdogService(
            store,
            probe,
            repair,
            config=WatchdogConfig(stations=(1, 2)),
            clock=lambda: 100.0,
        )
        report = await service.run_once()
        assert report.healthy == 1
        assert report.unhealthy == 1
        assert report.repaired == 1
        assert repair.calls == [(2, "tunnel_down")]
        assert store.station_state(1)["state"] == "USABLE"
        assert store.station_state(2)["state"] == "REPAIRING"
        assert store.station_state(2)["reason"] == "tunnel down"
        assert store.open_incidents(station=2) == []
        assert store.open_incidents(station=1) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_repair_failure_escalates_after_max_attempts(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        probe = FakeProbe({2: tunnel_down(2)})
        repair = FakeRepair(False)
        service = WatchdogService(
            store,
            probe,
            repair,
            config=WatchdogConfig(stations=(2,), max_repair_attempts=2),
            clock=lambda: 100.0,
        )
        await service.run_once()
        await service.run_once()
        report = await service.run_once()
        assert report.escalated == 1
        assert len(repair.calls) == 2
        incidents = store.open_incidents(station=2)
        assert incidents == []
        row = store._conn.execute("SELECT remediation FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
        assert row["remediation"] == "escalated"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_probe_is_disabled_not_unhealthy(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    try:
        service = WatchdogService(
            store,
            None,
            None,
            config=WatchdogConfig(stations=(1, 2)),
            clock=lambda: 100.0,
        )
        report = await service.run_once(now=100.0)
        assert report.probed == 0
        assert report.unhealthy == 0
        state = store.watchdog_state()
        assert state is not None
        assert state["status"] == "disabled"
        assert store.open_incidents() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_repair_backoff_skips_repeated_repairs(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    repair = FakeRepair()
    probe = FakeProbe({2: tunnel_down(2)})
    try:
        service = WatchdogService(
            store,
            probe,
            repair,
            config=WatchdogConfig(
                stations=(2,),
                max_repair_attempts=10,
                repair_backoff_s=300.0,
            ),
            clock=lambda: 100.0,
        )
        await service.run_once(now=100.0)
        await service.run_once(now=200.0)
        assert len(repair.calls) == 1
        await service.run_once(now=400.0)
        assert len(repair.calls) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_healthy_probe_closes_open_incident(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        observations = {2: tunnel_down(2)}
        probe = FakeProbe(observations)
        service = WatchdogService(
            store,
            probe,
            FakeRepair(False),
            config=WatchdogConfig(stations=(2,)),
            clock=lambda: 100.0,
        )
        await service.run_once()
        assert len(store.open_incidents(station=2)) == 1
        observations[2] = healthy(2)
        await service.run_once()
        assert store.open_incidents(station=2) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_probe_exception_is_a_station_incident_not_a_crash(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        probe = FakeProbe({2: RuntimeError("boom")})
        service = WatchdogService(store, probe, None, config=WatchdogConfig(stations=(2,)))
        report = await service.run_once()
        assert report.unhealthy == 1
        state = store.station_state(2)
        assert state is not None
        assert state["state"] == "REPAIRING"
        assert "RuntimeError" in state["reason"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_once_records_heartbeat_and_status(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        probe = FakeProbe({1: healthy(1)})
        service = WatchdogService(
            store,
            probe,
            None,
            config=WatchdogConfig(stations=(1,)),
            clock=lambda: 100.0,
        )
        await service.run_once()
        heartbeat = store.watchdog_state()
        assert heartbeat is not None
        assert heartbeat["heartbeat_at"] == 100.0
        assert heartbeat["status"] == "ok"
        assert service.is_stale(now=150.0, max_age_s=90.0) is False
        assert service.is_stale(now=191.0, max_age_s=90.0) is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_watchdog_state_persists_after_reopen(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        probe = FakeProbe({1: tunnel_down(1)})
        service = WatchdogService(
            store,
            probe,
            None,
            config=WatchdogConfig(stations=(1,)),
            clock=lambda: 100.0,
        )
        await service.run_once()
        assert store.open_incidents(station=1)
    finally:
        store.close()
    reopened = QualificationStore(db)
    try:
        state = reopened.station_state(1)
        assert state is not None
        assert state["state"] == "REPAIRING"
        heartbeat = reopened.watchdog_state()
        assert heartbeat is not None
        assert heartbeat["heartbeat_at"] == 100.0
        assert reopened.open_incidents(station=1)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_repair_failure_marks_heartbeat_partial(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        probe = FakeProbe({2: tunnel_down(2)})
        service = WatchdogService(
            store,
            probe,
            FakeRepair(False),
            config=WatchdogConfig(stations=(2,), max_repair_attempts=3),
            clock=lambda: 100.0,
        )
        report = await service.run_once()
        assert report.errors
        heartbeat = store.watchdog_state()
        assert heartbeat is not None
        assert heartbeat["status"] == "partial"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_watchdog_writes_lifecycle_state_on_production_schema(tmp_path):
    import sqlite3
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    schema = (root / "rotation" / "infrastructure" / "db" / "schema.sql").read_text(
        encoding="utf-8"
    )
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.close()

    store = QualificationStore(db)
    try:
        probe = FakeProbe({1: healthy(1), 2: tunnel_down(2)})
        service = WatchdogService(
            store,
            probe,
            config=WatchdogConfig(stations=(1, 2)),
            clock=lambda: 100.0,
        )
        report = await service.run_once()
        assert report.healthy == 1
        assert report.unhealthy == 1
        rows = {row["station"]: row for row in store.list_station_states()}
        assert rows[1]["state"] == "USABLE"
        assert rows[1]["client_usable"] is True
        assert rows[2]["state"] == "REPAIRING"
        assert rows[2]["client_usable"] is False
        assert rows[2]["reason"] == "tunnel down"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_quota_station_is_draining_not_an_incident(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        quota = StationObservation(
            station=3,
            connected=True,
            dns_ok=True,
            egress_ip="3.3.3.3",
            client_usable=False,
            reason="daily-bucket-exhausted",
        )
        probe = FakeProbe({3: quota})
        repair = FakeRepair(True)
        service = WatchdogService(
            store,
            probe,
            repair,
            config=WatchdogConfig(stations=(3,)),
            clock=lambda: 100.0,
        )
        report = await service.run_once()
        assert report.draining == 1
        assert report.unhealthy == 0
        assert repair.calls == []
        assert store.open_incidents(station=3) == []
        assert store.station_state(3)["state"] == "DRAINING"
        assert store.station_state(3)["client_usable"] is False
    finally:
        store.close()
