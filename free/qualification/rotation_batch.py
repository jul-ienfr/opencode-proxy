"""Bounded parallel execution of per-station rotation decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .rotation import StationSnapshot
from .rotation_executor import RotationExecutor, RotationResult


@dataclass
class RotationBatchResult:
    results: list[RotationResult]

    def snapshot(self) -> dict[str, Any]:
        return {"results": [item.snapshot() for item in self.results]}


class RotationBatch:
    def __init__(
        self,
        executor: RotationExecutor,
        *,
        max_concurrent: int = 6,
    ) -> None:
        self.executor = executor
        self.max_concurrent = max(1, int(max_concurrent))

    async def run(
        self,
        snapshots: Iterable[StationSnapshot],
        *,
        usable_stations: int = 6,
        exclude_ips: set[str] | None = None,
        now: float | None = None,
    ) -> RotationBatchResult:
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def one(snapshot: StationSnapshot) -> RotationResult:
            async with semaphore:
                return await self.executor.execute(
                    snapshot,
                    usable_stations=usable_stations,
                    exclude_ips=exclude_ips,
                    now=now,
                )

        results = await asyncio.gather(*(one(item) for item in snapshots))
        return RotationBatchResult(results=list(results))


def build_rotation_batch(
    executor: RotationExecutor,
    config: dict[str, Any],
    *,
    max_concurrent: int = 6,
) -> RotationBatch:
    """Build a batch honoring ``parallel_rotations`` (default true)."""

    catalog = config.get("egress_catalog") if isinstance(config, dict) else {}
    qualification = (
        catalog.get("qualification") if isinstance(catalog, dict) else {}
    )
    enabled = bool((qualification or {}).get("parallel_rotations", True))
    return RotationBatch(
        executor,
        max_concurrent=max_concurrent if enabled else 1,
    )


__all__ = ["RotationBatch", "RotationBatchResult", "build_rotation_batch"]
