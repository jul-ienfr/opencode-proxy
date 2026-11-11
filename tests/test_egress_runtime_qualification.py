"""EgressRuntime lifecycle wiring for the qualification scheduler."""

from __future__ import annotations

import pytest

from free.egress_runtime import EgressRuntime


class FakeStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeEgressScheduler:
    def __init__(self):
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    async def close(self):
        self.closed = True


class FakeQualificationScheduler:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_runtime_starts_and_stops_qualification_scheduler():
    store = FakeStore()
    egress = FakeEgressScheduler()
    qualification = FakeQualificationScheduler()
    runtime = EgressRuntime(
        store=store,
        scheduler=egress,
        qualification_scheduler=qualification,
    )
    await runtime.start()
    assert egress.started is True
    assert qualification.started is True
    await runtime.close()
    assert qualification.stopped is True
    assert egress.closed is True
    assert store.closed is True
