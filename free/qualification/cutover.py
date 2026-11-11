"""Make-before-break cutover for one station.

The service never releases the current IP before the target has been prepared.
If the target switch or probe fails, the old path is restored.  The actual
binder/probe/traffic switch are injected; tests use fakes, production uses the
station adapter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .store import QualificationStore


@dataclass(frozen=True, slots=True)
class CutoverTarget:
    station: int
    to_option_key: str = ""
    to_egress_ip: str = ""
    to_transport: str = ""
    from_option_key: str = ""
    from_egress_ip: str = ""
    from_transport: str = ""
    standby: bool = False


class CutoverPorts(Protocol):
    async def prepare(self, target: CutoverTarget) -> bool: ...

    async def switch(self, target: CutoverTarget) -> bool: ...

    async def probe(self, target: CutoverTarget) -> bool: ...

    async def rollback(self, target: CutoverTarget) -> bool: ...


@dataclass(frozen=True, slots=True)
class CutoverResult:
    station: int
    status: str
    duration_ms: float
    reason: str = ""
    cutover_id: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "reason": self.reason,
            "cutover_id": self.cutover_id,
        }


class CutoverService:
    def __init__(
        self,
        store: QualificationStore,
        ports: CutoverPorts,
        *,
        timeout_s: float = 5.0,
        clock=time.time,
    ) -> None:
        self.store = store
        self.ports = ports
        self.timeout_s = max(0.1, float(timeout_s))
        self._clock = clock

    async def _call(self, method, target: CutoverTarget) -> bool:
        return bool(
            await asyncio.wait_for(
                method(target),
                timeout=self.timeout_s,
            )
        )

    async def execute(self, target: CutoverTarget) -> CutoverResult:
        started = self._clock()
        try:
            prepared = await self._call(self.ports.prepare, target)
        except Exception as exc:
            return self._finish(target, started, "failed", f"prepare:{type(exc).__name__}")
        if not prepared:
            return self._finish(target, started, "failed", "prepare-failed")

        switched = False
        try:
            switched = await self._call(self.ports.switch, target)
        except Exception as exc:
            switched = False
            reason = f"switch:{type(exc).__name__}"
        else:
            reason = "switch-failed" if not switched else ""

        if not switched:
            await self._rollback(target)
            return self._finish(target, started, "rolled-back", reason or "switch-failed")

        try:
            proven = await self._call(self.ports.probe, target)
        except Exception as exc:
            proven = False
            reason = f"probe:{type(exc).__name__}"
        else:
            reason = "probe-failed" if not proven else ""

        if not proven:
            await self._rollback(target)
            return self._finish(target, started, "rolled-back", reason or "probe-failed")

        finished = self._clock()
        duration_ms = max(0.0, (finished - started) * 1000.0)
        cutover_id = self.store.record_cutover(
            station=int(target.station),
            from_option_key=target.from_option_key,
            from_egress_ip=target.from_egress_ip,
            to_option_key=target.to_option_key,
            to_egress_ip=target.to_egress_ip,
            transport=target.to_transport,
            started_at=started,
            committed_at=finished,
            finished_at=finished,
            duration_ms=duration_ms,
            result="committed",
            reason="",
        )
        return CutoverResult(
            station=int(target.station),
            status="committed",
            duration_ms=duration_ms,
            cutover_id=cutover_id,
        )

    async def _rollback(self, target: CutoverTarget) -> None:
        try:
            await self._call(self.ports.rollback, target)
        except Exception:
            pass

    def _finish(
        self,
        target: CutoverTarget,
        started: float,
        status: str,
        reason: str,
    ) -> CutoverResult:
        finished = self._clock()
        duration_ms = max(0.0, (finished - started) * 1000.0)
        cutover_id = self.store.record_cutover(
            station=int(target.station),
            from_option_key=target.from_option_key,
            from_egress_ip=target.from_egress_ip,
            to_option_key=target.to_option_key,
            to_egress_ip=target.to_egress_ip,
            transport=target.to_transport,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            result=status,
            reason=reason,
        )
        return CutoverResult(
            station=int(target.station),
            status=status,
            duration_ms=duration_ms,
            reason=reason,
            cutover_id=cutover_id,
        )


__all__ = ["CutoverPorts", "CutoverResult", "CutoverService", "CutoverTarget"]
