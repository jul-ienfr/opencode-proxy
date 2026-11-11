"""Asyncio scheduler for the egress catalog.

One loop per provider, single-flight per provider, jittered cadence and
fail-soft runs.  It never sits in the request path: the dashboard only reads
the catalog and may ask for an explicit refresh.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass

from free.egress_adapters.base import EgressAdapter, RefreshResult
from free.egress_catalog import ProviderPolicy
from free.egress_store import EgressStore

LOGGER = logging.getLogger("egress-catalog")


@dataclass
class ProviderRuntime:
    adapter: EgressAdapter
    policy: ProviderPolicy
    interval_s: float
    next_run_at: float = 0.0
    last_result: RefreshResult | None = None
    last_error: str = ""


class EgressScheduler:
    def __init__(
        self,
        store: EgressStore,
        providers: Mapping[str, tuple[EgressAdapter, ProviderPolicy, float]],
        *,
        jitter_pct: float = 15.0,
        clock=time.time,
        random_fn=random.uniform,
    ) -> None:
        self.store = store
        self._providers = {
            str(name): ProviderRuntime(
                adapter=adapter,
                policy=policy,
                interval_s=max(60.0, float(interval)),
                next_run_at=float((clock or time.time)()),
            )
            for name, (adapter, policy, interval) in providers.items()
        }
        self._jitter = max(0.0, min(90.0, float(jitter_pct))) / 100.0
        self._clock = clock or time.time
        self._random = random_fn or random.uniform
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {
            name: asyncio.Lock() for name in self._providers
        }
        self._closed = False
        self.on_run = None

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    def start(self) -> None:
        if self._closed or self._tasks:
            return
        for name in self._providers:
            self._tasks[name] = asyncio.create_task(
                self._loop(name), name=f"egress-catalog:{name}"
            )

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def refresh_now(self, provider: str) -> RefreshResult | None:
        name = str(provider or "").strip().lower()
        runtime = self._providers.get(name)
        if runtime is None:
            return None
        return await self._run(name, runtime)

    async def _loop(self, name: str) -> None:
        runtime = self._providers[name]
        while not self._closed:
            delay = max(1.0, runtime.next_run_at - self._clock())
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            if self._closed:
                break
            try:
                await self._run(name, runtime)
            except Exception as exc:
                runtime.last_error = f"{type(exc).__name__}: {exc}"[:300]
                LOGGER.warning("boucle %s: %s", name, runtime.last_error)
            jitter = 1.0 + self._random(-self._jitter, self._jitter) if self._jitter else 1.0
            runtime.next_run_at = self._clock() + max(60.0, runtime.interval_s * jitter)

    async def _run(self, name: str, runtime: ProviderRuntime) -> RefreshResult:
        lock = self._locks[name]
        if lock.locked():
            # Single-flight: return the previous result rather than queueing
            # an unbounded number of refresh requests from the UI.
            return runtime.last_result or RefreshResult(provider=name)
        async with lock:
            try:
                run_id = self.store.start_run(name)
            except Exception as exc:
                message = f"run non créé: {type(exc).__name__}: {exc}"[:300]
                runtime.last_error = message
                runtime.last_result = RefreshResult(provider=name, errors=[message])
                LOGGER.warning("%s: %s", name, message)
                return runtime.last_result
            try:
                result = await runtime.adapter.refresh(
                    self.store,
                    runtime.policy,
                    run_id=run_id,
                    observe_only=False,
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                runtime.last_error = message
                runtime.last_result = RefreshResult(provider=name, errors=[message])
                try:
                    self.store.finish_run(run_id, status="failed", errors=[message])
                except Exception:
                    LOGGER.exception("finish_run %s", name)
                LOGGER.warning("run %s échoué: %s", name, message)
                return runtime.last_result
            runtime.last_error = ""
            runtime.last_result = result
            status = "partial" if result.errors else "ok"
            try:
                self.store.finish_run(
                    run_id,
                    status=status,
                    candidates=result.candidates,
                    promoted=result.promoted,
                    quarantined=result.quarantined,
                    dead=result.dead,
                    errors=result.errors,
                )
            except Exception:
                LOGGER.exception("finish_run %s", name)
            LOGGER.info(
                "%s candidates=%s usable=%s quota=%s dead=%s skipped=%s",
                name,
                result.candidates,
                result.promoted,
                result.quarantined,
                result.dead,
                result.skipped,
            )
            if callable(self.on_run):
                try:
                    value = self.on_run(name, result)
                    if asyncio.iscoroutine(value):
                        await value
                except Exception as exc:
                    LOGGER.warning("on_run %s: %s", name, exc)
            return result


def build_scheduler(
    store: EgressStore,
    providers: Mapping[str, tuple[EgressAdapter, ProviderPolicy, float]],
    *,
    jitter_pct: float = 15.0,
) -> EgressScheduler:
    return EgressScheduler(store, providers, jitter_pct=jitter_pct)


__all__ = ["EgressScheduler", "ProviderRuntime", "build_scheduler"]
