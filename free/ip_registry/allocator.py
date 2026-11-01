"""Station allocator: needs → claim → bind → probe → assign/freeze.

The allocator is deliberately small and dependency-injected.  It never talks
to Docker, curl or SQLite directly: it uses the ports.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .domain import GeoFilter, NeedReason
from .ports import IpRegistry, ProbeClient, StationBinder


@dataclass(frozen=True, slots=True)
class StationNeed:
    station: int
    reason: NeedReason
    geo: GeoFilter | None = None


@dataclass(frozen=True, slots=True)
class AllocationOutcome:
    station: int
    ok: bool
    egress_ip: str | None = None
    hostname: str | None = None
    attempts: int = 0
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationReport:
    outcomes: tuple[AllocationOutcome, ...] = field(default_factory=tuple)

    @property
    def ok_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def failed(self) -> tuple[AllocationOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.ok)


class IpAllocator:
    """Allocate one IP per station that needs it."""

    def __init__(
        self,
        registry: IpRegistry,
        binder: StationBinder,
        probe: ProbeClient,
        *,
        model: str,
        lease_ttl_s: float = 300.0,
        max_attempts: int = 20,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._binder = binder
        self._probe = probe
        self._model = model
        self._lease_ttl_s = max(1.0, float(lease_ttl_s))
        self._max_attempts = max(1, int(max_attempts))
        self._clock = clock

    async def run(self, needs: Iterable[StationNeed]) -> AllocationReport:
        outcomes = tuple([await self.allocate(need) for need in needs])
        return AllocationReport(outcomes=outcomes)

    async def allocate(self, need: StationNeed) -> AllocationOutcome:
        now = self._clock()
        candidates = self._registry.eligible(
            geo=need.geo,
            now=now,
            limit=self._max_attempts,
        )
        attempts = 0
        last_reason = "no-candidate" if not candidates else "all-candidates-failed"
        for candidate in candidates:
            attempts += 1
            lease = self._registry.claim(
                candidate.egress_ip,
                station=need.station,
                ttl_s=self._lease_ttl_s,
                now=now,
            )
            if lease is None:
                last_reason = "claim-lost"
                continue
            try:
                bind = await self._binder.assign(need.station, candidate.hostname)
            except Exception as exc:
                self._registry.release(lease, "bind_exception", now)
                last_reason = f"bind_exception:{type(exc).__name__}"
                now = self._clock()
                continue
            if not bind.ok:
                self._registry.release(lease, "bind_error", now)
                last_reason = f"bind_error:{bind.error or 'unknown'}"
                now = self._clock()
                continue
            if bind.egress_ip and bind.egress_ip != candidate.egress_ip:
                self._registry.release(lease, "egress_mismatch", now)
                last_reason = (
                    f"egress_mismatch:{candidate.egress_ip}->{bind.egress_ip}"
                )
                now = self._clock()
                continue
            try:
                result = await self._probe.probe(need.station, self._model)
            except Exception as exc:
                self._registry.release(lease, "probe_exception", now)
                last_reason = f"probe_exception:{type(exc).__name__}"
                now = self._clock()
                continue
            self._registry.record(lease, result)
            if result.ok:
                assigned = self._registry.assign(lease, now)
                if assigned is not None:
                    return AllocationOutcome(
                        station=need.station,
                        ok=True,
                        egress_ip=assigned.egress_ip,
                        hostname=assigned.hostname,
                        attempts=attempts,
                    )
                self._registry.release(lease, "assign_error", now)
                last_reason = "assign_error"
            elif result.status == 429:
                self._registry.release(lease, "quota_429", now)
                last_reason = "quota_429"
            else:
                self._registry.release(lease, "probe_error", now)
                last_reason = f"probe_error:{result.status}"
            now = self._clock()
        return AllocationOutcome(
            station=need.station,
            ok=False,
            attempts=attempts,
            reason=last_reason,
        )
