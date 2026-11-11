"""Qualification scheduler tests: cycle order, error containment, stop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from free.qualification.scheduler import (
    QualificationScheduler,
    QualificationSchedulerConfig,
)


@dataclass
class FakeReport:
    status: str = "ok"
    quota: int = 0


class FakeDiscovery:
    def __init__(self):
        self.calls = 0
        self.due = True

    def is_due(self, *, now=None):
        return self.due

    def run(self, *, now=None, force=False):
        self.calls += 1
        return FakeReport()


class FakeWatchdog:
    def __init__(self):
        self.calls = 0

    async def run_once(self, *, now=None):
        self.calls += 1
        return FakeReport()


class FakeSweep:
    def __init__(self, error=None, *, quota=0):
        self.calls = 0
        self.error = error
        self.quota = quota

    async def run_epoch(self, *, epoch_id="", max_hosts=None, shadow=None):
        self.calls += 1
        if self.error:
            raise self.error
        return FakeReport(quota=self.quota)


class FakePreflight:
    def __init__(self):
        self.calls = 0

    async def run(self, *, limit=500, transport="wireguard", now=None):
        self.calls += 1
        return FakeReport()


@pytest.mark.asyncio
async def test_run_once_orders_discovery_watchdog_sweep():
    order: list[str] = []
    discovery = FakeDiscovery()
    watchdog = FakeWatchdog()
    preflight = FakePreflight()
    sweep = FakeSweep()
    original_discovery = discovery.run
    original_watchdog = watchdog.run_once
    original_preflight = preflight.run
    original_sweep = sweep.run_epoch

    def discovery_run(*args, **kwargs):
        order.append("discovery")
        return original_discovery(*args, **kwargs)

    async def watchdog_run(*args, **kwargs):
        order.append("watchdog")
        return await original_watchdog(*args, **kwargs)

    async def preflight_run(*args, **kwargs):
        order.append("preflight")
        return await original_preflight(*args, **kwargs)

    async def sweep_run(*args, **kwargs):
        order.append("sweep")
        return await original_sweep(*args, **kwargs)

    discovery.run = discovery_run
    watchdog.run_once = watchdog_run
    preflight.run = preflight_run
    sweep.run_epoch = sweep_run
    scheduler = QualificationScheduler(
        sweep,
        discovery=discovery,
        watchdog=watchdog,
        preflight=preflight,
        clock=lambda: 100.0,
    )
    report = await scheduler.run_once(now=100.0)
    assert order == ["discovery", "watchdog", "preflight", "sweep"]
    assert report["errors"] == []
    assert discovery.calls == 1
    assert watchdog.calls == 1
    assert preflight.calls == 1
    assert sweep.calls == 1


@pytest.mark.asyncio
async def test_watchdog_interval_is_respected():
    watchdog = FakeWatchdog()
    scheduler = QualificationScheduler(
        FakeSweep(),
        watchdog=watchdog,
        config=QualificationSchedulerConfig(
            run_discovery=False,
            run_preflight=False,
            watchdog_interval_s=1000.0,
        ),
        clock=lambda: 100.0,
    )
    await scheduler.run_once(now=100.0)
    await scheduler.run_once(now=200.0)
    assert watchdog.calls == 1
    await scheduler.run_once(now=1200.0)
    assert watchdog.calls == 2


@pytest.mark.asyncio
async def test_global_pause_on_429_is_opt_in():
    sweep = FakeSweep(quota=1)
    scheduler = QualificationScheduler(
        sweep,
        config=QualificationSchedulerConfig(
            run_discovery=False,
            run_preflight=False,
            run_watchdog=False,
            global_pause_on_429=True,
            global_pause_s=60.0,
        ),
        clock=lambda: 100.0,
    )
    first = await scheduler.run_once(now=100.0)
    assert first["paused_until"] == 160.0
    second = await scheduler.run_once(now=120.0)
    assert second["paused"] is True
    assert sweep.calls == 1
    resumed = await scheduler.run_once(now=160.0)
    assert "paused" not in resumed
    assert sweep.calls == 2


@pytest.mark.asyncio
async def test_multiple_sweeps_run_concurrently():
    first = FakeSweep()
    second = FakeSweep()
    scheduler = QualificationScheduler(
        sweeps=[first, second],
        config=QualificationSchedulerConfig(
            run_discovery=False,
            run_preflight=False,
            run_watchdog=False,
        ),
        clock=lambda: 100.0,
    )
    report = await scheduler.run_once(now=100.0)
    assert first.calls == 1
    assert second.calls == 1
    assert len(report["sweeps"]) == 2
    assert report["errors"] == []


@pytest.mark.asyncio
async def test_component_errors_are_contained():
    sweep = FakeSweep(error=RuntimeError("sweep down"))
    scheduler = QualificationScheduler(
        sweep,
        discovery=FakeDiscovery(),
        watchdog=FakeWatchdog(),
        config=QualificationSchedulerConfig(shadow=False),
        clock=lambda: 100.0,
    )
    report = await scheduler.run_once(now=100.0)
    assert "sweep:RuntimeError" in report["errors"]
    assert report["finished_at"] == 100.0


@pytest.mark.asyncio
async def test_start_and_stop_are_clean():
    sweep = FakeSweep()
    scheduler = QualificationScheduler(
        sweep,
        config=QualificationSchedulerConfig(interval_s=3600.0),
        clock=lambda: 100.0,
    )
    scheduler.start()
    assert scheduler.running
    await asyncio.sleep(0.05)
    assert sweep.calls == 1
    await scheduler.stop()
    assert not scheduler.running
    await asyncio.sleep(0.05)
    assert sweep.calls == 1


class StationSweep:
    def __init__(self, station):
        self.calls = 0
        self.config = SimpleNamespace(station=station)

    async def run_epoch(self, *, epoch_id="", max_hosts=None, shadow=None):
        self.calls += 1
        return FakeReport()


@pytest.mark.asyncio
async def test_service_priority_skips_serving_station_in_live_mode():
    serving = StationSweep(1)
    idle = StationSweep(2)
    scheduler = QualificationScheduler(
        sweeps=[serving, idle],
        config=QualificationSchedulerConfig(
            run_discovery=False,
            run_preflight=False,
            run_watchdog=False,
            shadow=False,
        ),
        clock=lambda: 100.0,
        station_state_getter=lambda station: {
            1: "serving",
            2: "idle",
        }[station],
    )
    report = await scheduler.run_once(now=100.0)
    assert serving.calls == 0
    assert idle.calls == 1
    assert report["sweep"].status == "ok"
    assert report["skipped_sweeps"] == [
        {
            "station": 1,
            "skipped": True,
            "reason": "service-priority:serving",
        }
    ]


@pytest.mark.asyncio
async def test_service_priority_allows_shadow_and_disabled():
    for shadow, service_priority in ((True, True), (False, False)):
        first = StationSweep(1)
        second = StationSweep(2)
        scheduler = QualificationScheduler(
            sweeps=[first, second],
            config=QualificationSchedulerConfig(
                run_discovery=False,
                run_preflight=False,
                run_watchdog=False,
                shadow=shadow,
                service_priority=service_priority,
            ),
            clock=lambda: 100.0,
            station_state_getter=lambda station: "serving",
        )
        report = await scheduler.run_once(now=100.0)
        assert first.calls == 1
        assert second.calls == 1
        assert "skipped_sweeps" not in report


@pytest.mark.asyncio
async def test_service_priority_getter_failure_is_fail_open():
    first = StationSweep(1)
    scheduler = QualificationScheduler(
        sweeps=[first],
        config=QualificationSchedulerConfig(
            run_discovery=False,
            run_preflight=False,
            run_watchdog=False,
            shadow=False,
        ),
        clock=lambda: 100.0,
        station_state_getter=lambda station: (_ for _ in ()).throw(
            RuntimeError("db down")
        ),
    )
    report = await scheduler.run_once(now=100.0)
    assert first.calls == 1
    assert "skipped_sweeps" not in report
