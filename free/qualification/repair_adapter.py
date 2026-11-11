"""Bridge the watchdog to the existing per-station supervisor.

``ops.supervisor.StationSupervisor`` already owns restart/breaker logic.  This
adapter only calls ``restart`` for the affected station; it never creates a
second repair owner and never touches other stations.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class SupervisorLike(Protocol):
    async def restart(self, reason: str = "") -> None: ...


class SupervisorRepairAdapter:
    def __init__(self, supervisors: Mapping[int, SupervisorLike]) -> None:
        self.supervisors = dict(supervisors)

    async def repair(self, station: int, cause: str) -> bool:
        supervisor = self.supervisors.get(int(station))
        if supervisor is None:
            return False
        try:
            await supervisor.restart(reason=f"watchdog:{cause}")
        except Exception:
            return False
        return True


def build_supervisor_repair_adapter() -> SupervisorRepairAdapter | None:
    """Build from ``shared_state.station_supervisors`` when available."""

    try:
        import shared_state

        supervisors = getattr(shared_state, "station_supervisors", None) or []
    except Exception:
        return None
    mapping: dict[int, Any] = {}
    for supervisor in supervisors:
        try:
            mapping[int(supervisor.station)] = supervisor
        except (TypeError, ValueError, AttributeError):
            continue
    return SupervisorRepairAdapter(mapping) if mapping else None


__all__ = ["SupervisorRepairAdapter", "SupervisorLike", "build_supervisor_repair_adapter"]
