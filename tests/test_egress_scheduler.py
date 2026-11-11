"""Scheduler: single-flight, fail-soft, jitter and close."""

from __future__ import annotations

import asyncio

import pytest

from free.egress_adapters.base import RefreshResult
from free.egress_catalog import Provider, ProviderPolicy
from free.egress_scheduler import EgressScheduler
from free.egress_store import EgressStore


class _Adapter:
    provider = Provider.PUBLIC

    def __init__(self, *, result=None, error=None, gate=None):
        self.calls = 0
        self.result = result or RefreshResult(provider=self.provider)
        self.error = error
        self.gate = gate

    async def refresh(self, store, policy, *, run_id="", max_candidates=None, observe_only=False):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        return self.result


def _scheduler(tmp_path, adapter, *, interval=600.0, clock=None, random_fn=None):
    store = EgressStore(tmp_path / "registry.db")
    scheduler = EgressScheduler(
        store,
        {
            Provider.PUBLIC: (
                adapter,
                ProviderPolicy(provider=Provider.PUBLIC),
                interval,
            )
        },
        jitter_pct=15.0,
        clock=clock,
        random_fn=random_fn,
    )
    return store, scheduler


@pytest.mark.asyncio
async def test_refresh_now_runs_one_cycle(tmp_path):
    adapter = _Adapter(result=RefreshResult(provider=Provider.PUBLIC, candidates=2, promoted=1))
    store, scheduler = _scheduler(tmp_path, adapter)
    try:
        result = await scheduler.refresh_now(Provider.PUBLIC)
        assert result is not None and result.promoted == 1
        assert adapter.calls == 1
        assert store.list_runs(provider=Provider.PUBLIC)[0]["status"] == "ok"
    finally:
        await scheduler.close()
        store.close()


@pytest.mark.asyncio
async def test_single_flight_returns_previous_result(tmp_path):
    gate = asyncio.Event()
    adapter = _Adapter(
        result=RefreshResult(provider=Provider.PUBLIC, promoted=1),
        gate=gate,
    )
    store, scheduler = _scheduler(tmp_path, adapter)
    try:
        first = asyncio.create_task(scheduler.refresh_now(Provider.PUBLIC))
        await asyncio.sleep(0)
        second = await scheduler.refresh_now(Provider.PUBLIC)
        gate.set()
        awaited = await first
        assert adapter.calls == 1
        assert second.provider == Provider.PUBLIC
        assert awaited.promoted == 1
    finally:
        await scheduler.close()
        store.close()


@pytest.mark.asyncio
async def test_failed_cycle_is_fail_soft_and_keeps_catalog(tmp_path):
    adapter = _Adapter(error=RuntimeError("source down"))
    store, scheduler = _scheduler(tmp_path, adapter)
    try:
        result = await scheduler.refresh_now(Provider.PUBLIC)
        assert result is not None and result.errors
        run = store.list_runs(provider=Provider.PUBLIC)[0]
        assert run["status"] == "failed"
        assert store.list_options() == []
    finally:
        await scheduler.close()
        store.close()


@pytest.mark.asyncio
async def test_next_run_uses_jitter_and_never_goes_below_interval(tmp_path):
    now = [1_000.0]
    adapter = _Adapter()

    class _Stop(Exception):
        pass

    async def _refresh(store, policy, **kwargs):
        adapter.calls += 1
        scheduler._closed = True
        return RefreshResult(provider=Provider.PUBLIC)

    adapter.refresh = _refresh
    store, scheduler = _scheduler(
        tmp_path,
        adapter,
        interval=600.0,
        clock=lambda: now[0],
        random_fn=lambda low, high: high,
    )
    try:
        runtime = scheduler._providers[Provider.PUBLIC]
        runtime.next_run_at = now[0]
        import free.egress_scheduler as module

        original_sleep = module.asyncio.sleep

        async def _sleep(_delay):
            return None

        module.asyncio.sleep = _sleep
        try:
            await scheduler._loop(Provider.PUBLIC)
        finally:
            module.asyncio.sleep = original_sleep
        assert runtime.next_run_at == now[0] + 600.0 * 1.15
    finally:
        await scheduler.close()
        store.close()


@pytest.mark.asyncio
async def test_start_and_close_are_idempotent(tmp_path):
    adapter = _Adapter()
    store, scheduler = _scheduler(tmp_path, adapter, interval=600.0)
    try:
        scheduler.start()
        scheduler.start()
        assert len(scheduler._tasks) == 1
        await scheduler.close()
        assert scheduler._tasks == {}
    finally:
        store.close()
