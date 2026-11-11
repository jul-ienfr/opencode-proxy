"""Rotation launch guards for the free IP pool (T160).

The queueing itself stays in ``FreeIPPool``; this module owns the ordered
reasons a rotation must not be queued, so the decision is testable without
asyncio or Docker.
"""

from __future__ import annotations

FROZEN = "frozen"
UNKNOWN = "unknown"
PENDING = "pending"
RUNNING = "running"


def rotation_block_reason(
    *,
    upstream_active: bool,
    station_id: int,
    station_ids: set[int],
    pending: set[int],
    task_running: bool,
) -> str | None:
    """Return why a rotation cannot be queued, or ``None`` when it can.

    Order matters: an upstream event freezes every station before the retired
    and single-flight checks, matching the historical launch sequence.
    """

    if upstream_active:
        return FROZEN
    if station_id not in station_ids:
        return UNKNOWN
    if station_id in pending:
        return PENDING
    if task_running:
        return RUNNING
    return None


__all__ = [
    "FROZEN",
    "PENDING",
    "RUNNING",
    "UNKNOWN",
    "rotation_block_reason",
]
