"""Epoch lifecycle shared by :class:`QualificationStore`.

Kept as a mixin so ``store.py`` stays under the 1 500-line architecture gate:
the mixin expects the concrete store to provide ``_clock``, ``_conn``,
``_lock`` and ``summary()``.
"""

from __future__ import annotations

from typing import Any

from .domain import CandidateStatus


class EpochStoreMixin:
    def create_epoch(
        self, epoch_id: str, *, total: int = 0, now: float | None = None
    ) -> None:
        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO qualification_epochs(epoch_id, status, total, started_at)
                VALUES(?, 'running', ?, ?)
                ON CONFLICT(epoch_id) DO UPDATE SET status='running', started_at=excluded.started_at
                """,
                (str(epoch_id), int(total or 0), ts),
            )

    def close_epoch(
        self,
        epoch_id: str,
        *,
        status: str = "complete",
        now: float | None = None,
        reason: str = "",
        tested: int | None = None,
        usable: int | None = None,
        quota: int | None = None,
        infra: int | None = None,
        dead: int | None = None,
        skipped: int | None = None,
    ) -> None:
        """Close an epoch, optionally with the counters measured by that run.

        Historical callers omit the counters: the global summary is then
        written, which is only a fallback.  ``run_epoch`` always passes the
        per-run values so the epochs table does not report fleet totals as if
        they had been produced by the epoch itself.
        """

        ts = float(self._clock() if now is None else now)
        counters = (tested, usable, quota, infra, dead, skipped)
        if any(value is None for value in counters):
            summary: dict[str, Any] = self.summary()
            tested = int(summary["tested"])
            usable = int(summary["by_status"].get(CandidateStatus.USABLE, 0))
            quota = int(summary["by_status"].get(CandidateStatus.QUOTA, 0))
            infra = int(summary["infra"])
            dead = int(summary["by_status"].get(CandidateStatus.DEAD, 0))
            skipped = int(summary["by_status"].get(CandidateStatus.SKIPPED, 0))
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE qualification_epochs SET
                    status=?, tested=?, usable=?, quota=?, infra=?, dead=?, skipped=?,
                    finished_at=?, reason=?
                WHERE epoch_id=?
                """,
                (
                    str(status),
                    int(tested or 0),
                    int(usable or 0),
                    int(quota or 0),
                    int(infra or 0),
                    int(dead or 0),
                    int(skipped or 0),
                    ts,
                    str(reason or ""),
                    str(epoch_id),
                ),
            )

    def close_running_epochs(
        self,
        prefix: str,
        *,
        status: str = "partial",
        now: float | None = None,
        reason: str = "orphaned",
        except_id: str = "",
    ) -> int:
        """Close ``running`` epochs left behind by a crash or a killed slot.

        Epoch ids are namespaced per station by the qualifier
        (``qualifier-7-...``), so the prefix prevents one slot from closing
        another slot's live epoch.
        """

        ts = float(self._clock() if now is None else now)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE qualification_epochs
                SET status=?, finished_at=?, reason=?
                WHERE status='running' AND epoch_id LIKE ? AND epoch_id<>?
                """,
                (str(status), ts, str(reason or ""), f"{prefix}%", str(except_id or "")),
            )
            return int(cursor.rowcount or 0)
