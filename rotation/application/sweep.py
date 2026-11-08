"""Persistent sweep cursor.

The cursor lives in ``registry.db`` (``sweep_cursor`` table), so a supervisor
restart resumes at the next eligible hostname instead of resetting the scan to
zero. The application object is intentionally thin: the repository owns the
SQL, this class owns the scan policy (status, backoff, progress).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rotation.infrastructure.db.repositories import RegistryRepository


@dataclass(frozen=True)
class SweepProgress:
    total: int
    counts: dict[str, int]

    @property
    def tested(self) -> int:
        return self.total


class SweepCursor:
    def __init__(self, repo: RegistryRepository) -> None:
        self._repo = repo

    def record(
        self,
        *,
        batch_id: str,
        country: str,
        hostname: str,
        transport: str,
        status: str,
        next_eligible_at: float,
        tested_at: float | None = None,
    ) -> None:
        self._repo.record_sweep(
            batch_id=batch_id,
            country=country,
            hostname=hostname,
            transport=transport,
            status=str(status),
            next_eligible_at=float(next_eligible_at),
            tested_at=tested_at,
            failures=0 if str(status) == "ok" else 1,
        )

    def next(self, batch_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        return self._repo.next_sweep(batch_id, now=now)

    def progress(self, batch_id: str) -> SweepProgress:
        counts = self._repo.sweep_progress(batch_id)
        return SweepProgress(total=int(counts.pop("total", 0)), counts=counts)
