"""Per-station runtime and parallel worker orchestration.

Each station owns a worker and a ``station_runtime`` row.  Workers never share
a lock: a slow or failing station does not serialize the others.  The actual
sweep/cutover are injected, so the module is testable without Docker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .cutover import CutoverResult, CutoverService, CutoverTarget
from .store import QualificationStore

IDLE = "idle"
SERVING = "serving"
QUALIFYING = "qualifying"
ROTATING = "rotating"
REPAIRING = "repairing"
PAUSED = "paused"


class SweepLike(Protocol):
    async def run_epoch(self, *, epoch_id: str = "", max_hosts: int | None = None, shadow: bool | None = None): ...


@dataclass
class StationWorker:
    station: int
    store: QualificationStore
    sweep: SweepLike
    cutover: CutoverService | None = None
    clock: Callable[[], float] = time.time
    state: str = IDLE

    def __post_init__(self) -> None:
        current = self.store.station_runtime(self.station)
        if current is not None:
            self.state = str(current.get("state") or IDLE)

    def _persist(self, **changes: Any) -> None:
        self.store.upsert_station_runtime(
            self.station,
            state=self.state,
            last_reason=str(changes.get("last_reason") or ""),
            **{key: value for key, value in changes.items() if key != "last_reason"},
            now=self.clock(),
        )

    def pause(self, reason: str = "operator") -> None:
        self.state = PAUSED
        self._persist(last_reason=reason)

    def resume(self) -> None:
        if self.state == PAUSED:
            self.state = IDLE
            self._persist(last_reason="resumed")

    async def run_slice(
        self,
        *,
        epoch_id: str = "",
        max_hosts: int = 1,
        shadow: bool = False,
    ) -> dict[str, Any]:
        if self.state == PAUSED:
            return {"station": self.station, "state": self.state, "skipped": True}
        previous = self.state
        self.state = QUALIFYING
        self._persist(last_reason="sweep")
        try:
            result = await self.sweep.run_epoch(
                epoch_id=epoch_id,
                max_hosts=max(0, int(max_hosts)),
                shadow=shadow,
            )
            promoted = int(getattr(result, "promoted", 0) or 0)
            self.state = SERVING if promoted else IDLE
            self._persist(
                last_reason=f"slice promoted={promoted}",
                availability_1h=100.0 if promoted else 0.0,
            )
            return {
                "station": self.station,
                "state": self.state,
                "result": result.snapshot() if hasattr(result, "snapshot") else {},
                "skipped": False,
            }
        except Exception as exc:
            self.state = REPAIRING
            self._persist(last_reason=f"{type(exc).__name__}: {exc}")
            return {
                "station": self.station,
                "state": self.state,
                "error": f"{type(exc).__name__}: {exc}",
                "skipped": False,
            }
        finally:
            if self.state == QUALIFYING:
                self.state = previous

    async def rotate(self, target: CutoverTarget) -> CutoverResult:
        if self.cutover is None:
            raise RuntimeError("no-cutover-service")
        self.state = ROTATING
        self._persist(last_reason="cutover")
        result = await self.cutover.execute(target)
        self.state = SERVING if result.status == "committed" else IDLE
        self._persist(
            last_reason=result.reason or result.status,
            last_cutover_at=self.clock(),
            cutover_ms=result.duration_ms,
        )
        return result


@dataclass
class ParallelReport:
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {"results": list(self.results), "errors": list(self.errors)}


class ParallelStationRunner:
    def __init__(self, workers: list[StationWorker]) -> None:
        self.workers = list(workers)

    async def run(
        self,
        *,
        epoch_id: str = "",
        max_hosts: int = 1,
        shadow: bool = False,
    ) -> ParallelReport:
        report = ParallelReport()
        results = await asyncio.gather(
            *(
                worker.run_slice(epoch_id=epoch_id, max_hosts=max_hosts, shadow=shadow)
                for worker in self.workers
            ),
            return_exceptions=True,
        )
        for worker, value in zip(self.workers, results, strict=False):
            if isinstance(value, BaseException):
                report.errors.append(f"station-{worker.station}:{type(value).__name__}")
                continue
            if value.get("error"):
                report.errors.append(f"station-{worker.station}:{value['error']}")
            report.results.append(value)
        return report


__all__ = [
    "IDLE",
    "PAUSED",
    "QUALIFYING",
    "REPAIRING",
    "ROTATING",
    "SERVING",
    "ParallelReport",
    "ParallelStationRunner",
    "StationWorker",
]

