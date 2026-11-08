"""Warm egress pool: TTL, atomic promotion and replenishment target.

A warm spare is a verified egress that is not currently leased. Promotion is
atomic in the repository (SELECT + DELETE in one transaction), so two stations
can never take the same spare.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rotation.infrastructure.db.repositories import RegistryRepository


@dataclass(frozen=True)
class WarmPoolStatus:
    ready: int
    desired: int

    @property
    def missing(self) -> int:
        return max(0, self.desired - self.ready)


class WarmPool:
    def __init__(
        self,
        repo: RegistryRepository,
        *,
        desired: int = 9,
        ttl_s: float = 21600.0,
        clock=time.time,
    ) -> None:
        self._repo = repo
        self._desired = max(0, int(desired))
        self._ttl_s = max(60.0, float(ttl_s))
        self._clock = clock

    def put(
        self,
        *,
        ip: str,
        hostname: str,
        transport: str,
        model: str = "",
        score: float = 0.0,
    ) -> None:
        self._repo.put_warm_spare(
            ip=ip,
            hostname=hostname,
            transport=transport,
            model=model,
            ttl_s=self._ttl_s,
            score=score,
            now=self._clock(),
        )

    def take(self, *, transport: str | None = None) -> dict[str, Any] | None:
        return self._repo.take_warm_spare(
            transport=transport, now=self._clock()
        )

    def expire(self) -> int:
        return self._repo.expire_warm_spares(now=self._clock())

    def status(self) -> WarmPoolStatus:
        return WarmPoolStatus(
            ready=self._repo.count_warm_spares(now=self._clock()),
            desired=self._desired,
        )
