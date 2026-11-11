"""Parallel rotation batch tests."""

from __future__ import annotations

import asyncio

import pytest

from free.qualification.rotation import StationSnapshot
from free.qualification.rotation_batch import RotationBatch, build_rotation_batch


class FakeResult:
    def __init__(self, station):
        self.station = station

    def snapshot(self):
        return {"station": self.station}


class FakeExecutor:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def execute(self, snapshot, **kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return FakeResult(snapshot.station)


@pytest.mark.asyncio
async def test_batch_bounds_concurrency():
    executor = FakeExecutor()
    batch = RotationBatch(executor, max_concurrent=2)
    result = await batch.run(
        [StationSnapshot(station=index) for index in range(1, 7)]
    )
    assert len(result.results) == 6
    assert executor.calls == 6
    assert executor.max_active == 2


@pytest.mark.asyncio
async def test_batch_can_be_sequential():
    executor = FakeExecutor()
    batch = RotationBatch(executor, max_concurrent=1)
    await batch.run(
        [StationSnapshot(station=index) for index in range(1, 4)]
    )
    assert executor.max_active == 1


def test_build_batch_reads_parallel_rotations():
    executor = FakeExecutor()
    enabled = build_rotation_batch(executor, {}, max_concurrent=6)
    assert enabled.max_concurrent == 6
    disabled = build_rotation_batch(
        executor,
        {"egress_catalog": {"qualification": {"parallel_rotations": False}}},
        max_concurrent=6,
    )
    assert disabled.max_concurrent == 1
