"""Per-station repair orchestration.

The supervisor launches one chain per broken station. This object owns the
policy: at most one job per station, jobs for different stations run in
parallel up to ``max_parallel``, and a boot reconciliation closes jobs whose
station disappeared instead of keeping a phantom worker alive.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class RepairRequest:
    station: int
    cause: str = "repair"


@dataclass(frozen=True)
class RepairResult:
    station: int
    cause: str
    ok: bool
    duration_s: float
    error: str = ""


class RepairOrchestrator:
    def __init__(
        self,
        runner: Callable[[RepairRequest], Awaitable[bool]],
        *,
        max_parallel: int = 6,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._max_parallel = max(1, int(max_parallel))
        self._clock = clock

    async def repair(
        self, requests: Iterable[RepairRequest]
    ) -> list[RepairResult]:
        deduped: dict[int, RepairRequest] = {}
        for request in requests:
            station = int(request.station)
            if station > 0 and station not in deduped:
                deduped[station] = request
        semaphore = asyncio.Semaphore(self._max_parallel)

        async def one(request: RepairRequest) -> RepairResult:
            async with semaphore:
                started = self._clock()
                try:
                    ok = bool(await self._runner(request))
                    error = ""
                except Exception as exc:
                    ok = False
                    error = f"{type(exc).__name__}: {exc}"
                return RepairResult(
                    station=request.station,
                    cause=request.cause,
                    ok=ok,
                    duration_s=max(0.0, self._clock() - started),
                    error=error[:200],
                )

        return list(
            await asyncio.gather(*(one(request) for request in deduped.values()))
        )

    @staticmethod
    def reconcile(
        open_jobs: Iterable[dict],
        live_stations: Iterable[int],
    ) -> tuple[list[int], list[int]]:
        """Split open jobs into (keep, close) after a boot scan."""

        live = {int(station) for station in live_stations}
        keep: list[int] = []
        close: list[int] = []
        for job in open_jobs:
            try:
                station = int(job.get("station") or 0)
            except (TypeError, ValueError):
                continue
            (keep if station in live else close).append(station)
        return keep, close
