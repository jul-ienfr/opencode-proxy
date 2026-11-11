"""Per-station worker and parallel runner tests."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from free.qualification.cutover import CutoverResult, CutoverTarget
from free.qualification.station_runtime import (
    IDLE,
    PAUSED,
    SERVING,
    ParallelStationRunner,
    StationWorker,
)
from free.qualification.store import QualificationStore


@dataclass
class FakeSweepResult:
    promoted: int = 1

    def snapshot(self):
        return {"promoted": self.promoted}


class FakeSweep:
    def __init__(self, *, delay=0.0, error=None, promoted=1):
        self.delay = delay
        self.error = error
        self.promoted = promoted
        self.calls = []

    async def run_epoch(self, *, epoch_id="", max_hosts=None, shadow=None):
        self.calls.append((epoch_id, max_hosts, shadow))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return FakeSweepResult(self.promoted)


class FakeCutover:
    async def execute(self, target: CutoverTarget) -> CutoverResult:
        return CutoverResult(station=target.station, status="committed", duration_ms=12.0)


@pytest.mark.asyncio
async def test_two_workers_run_in_parallel_and_persist_state(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        w1 = StationWorker(1, store, FakeSweep(delay=0.05), clock=lambda: 100.0)
        w2 = StationWorker(2, store, FakeSweep(delay=0.05), clock=lambda: 100.0)
        started = time.monotonic()
        report = await ParallelStationRunner([w1, w2]).run(max_hosts=1)
        elapsed = time.monotonic() - started
        assert len(report.results) == 2
        assert report.errors == []
        assert elapsed < 0.15
        assert store.station_runtime(1)["state"] == SERVING
        assert store.station_runtime(2)["state"] == SERVING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_six_workers_run_in_parallel_with_unique_stations(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        workers = [
            StationWorker(station, store, FakeSweep(delay=0.05), clock=lambda: 100.0)
            for station in range(1, 7)
        ]
        started = time.monotonic()
        report = await ParallelStationRunner(workers).run(max_hosts=1)
        elapsed = time.monotonic() - started
        assert len(report.results) == 6
        assert report.errors == []
        assert elapsed < 0.18
        stations = {item["station"] for item in report.results}
        assert stations == {1, 2, 3, 4, 5, 6}
        for station in range(1, 7):
            assert store.station_runtime(station)["state"] == SERVING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_one_station_failure_does_not_cancel_the_other(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        w1 = StationWorker(1, store, FakeSweep(error=RuntimeError("boom")), clock=lambda: 100.0)
        w2 = StationWorker(2, store, FakeSweep(), clock=lambda: 100.0)
        report = await ParallelStationRunner([w1, w2]).run(max_hosts=1)
        assert len(report.results) == 2
        assert any("station-1" in item for item in report.errors)
        assert store.station_runtime(1)["state"] == "repairing"
        assert store.station_runtime(2)["state"] == SERVING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_paused_worker_does_not_call_sweep(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        sweep = FakeSweep()
        worker = StationWorker(1, store, sweep, clock=lambda: 100.0)
        worker.pause("operator")
        result = await worker.run_slice(max_hosts=1)
        assert result["skipped"] is True
        assert sweep.calls == []
        assert store.station_runtime(1)["state"] == PAUSED
        worker.resume()
        assert worker.state == IDLE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rotate_uses_cutover_and_records_duration(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        worker = StationWorker(
            1,
            store,
            FakeSweep(),
            cutover=FakeCutover(),
            clock=lambda: 100.0,
        )
        result = await worker.rotate(CutoverTarget(station=1, to_egress_ip="2.2.2.2"))
        assert result.status == "committed"
        runtime = store.station_runtime(1)
        assert runtime["state"] == SERVING
        assert runtime["cutover_ms"] == 12.0
    finally:
        store.close()
