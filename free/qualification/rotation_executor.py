"""Execute a rotation decision through the make-before-break cutover."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .cutover import CutoverResult, CutoverService, CutoverTarget
from .rotation import (
    KEEP,
    PROMOTE_STANDBY,
    PROTECT_LAST,
    ROTATE,
    WAIT,
    RotationDecision,
    RotationPlanner,
    StationSnapshot,
)
from .store import QualificationStore


@dataclass(frozen=True)
class RotationResult:
    decision: RotationDecision
    cutover: CutoverResult | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.decision.snapshot(),
            "cutover": self.cutover.snapshot() if self.cutover is not None else None,
        }


class RotationExecutor:
    def __init__(
        self,
        store: QualificationStore,
        planner: RotationPlanner,
        cutover: CutoverService,
        *,
        clock=time.time,
    ) -> None:
        self.store = store
        self.planner = planner
        self.cutover = cutover
        self._clock = clock

    async def execute(
        self,
        snapshot: StationSnapshot,
        *,
        usable_stations: int = 6,
        exclude_ips: set[str] | None = None,
        now: float | None = None,
    ) -> RotationResult:
        ts = float(self._clock() if now is None else now)
        decision = self.planner.plan(
            snapshot,
            usable_stations=usable_stations,
            exclude_ips=exclude_ips,
            now=ts,
        )
        if decision.action in {KEEP, WAIT, PROTECT_LAST}:
            return RotationResult(decision=decision)
        target = self._target(snapshot, decision)
        if target is None:
            return RotationResult(decision=decision)
        cutover = await self.cutover.execute(target)
        if cutover.status == "committed":
            self.store.upsert_station_runtime(
                snapshot.station,
                state="serving",
                current_option_key=target.to_option_key,
                current_egress_ip=target.to_egress_ip,
                current_transport=target.to_transport,
                last_cutover_at=ts,
                cutover_ms=cutover.duration_ms,
                last_reason="rotation-committed",
                now=ts,
            )
        else:
            self.store.upsert_station_runtime(
                snapshot.station,
                state="idle" if cutover.status != "committed" else "serving",
                last_reason=cutover.reason or cutover.status,
                now=ts,
            )
        return RotationResult(decision=decision, cutover=cutover)

    @staticmethod
    def _target(snapshot: StationSnapshot, decision: RotationDecision) -> CutoverTarget | None:
        if decision.action == PROMOTE_STANDBY:
            if not snapshot.standby_ip:
                return None
            return CutoverTarget(
                station=snapshot.station,
                from_option_key=snapshot.current_hostname or snapshot.current_ip,
                from_egress_ip=snapshot.current_ip,
                from_transport="",
                to_option_key=snapshot.standby_hostname or snapshot.standby_ip,
                to_egress_ip=snapshot.standby_ip,
                to_transport="",
                standby=True,
            )
        if decision.action == ROTATE and decision.candidate is not None:
            candidate = decision.candidate
            return CutoverTarget(
                station=snapshot.station,
                from_option_key=snapshot.current_hostname or snapshot.current_ip,
                from_egress_ip=snapshot.current_ip,
                from_transport="",
                to_option_key=candidate.hostname,
                to_egress_ip=candidate.last_egress_ip,
                to_transport=candidate.transports[0] if candidate.transports else "",
                standby=False,
            )
        return None


__all__ = ["RotationExecutor", "RotationResult"]
