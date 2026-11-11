"""Repair-job queries shared by the watchdog and the store."""

from __future__ import annotations

import sqlite3


def last_repair_at(
    conn: sqlite3.Connection,
    station: int,
    cause: str,
) -> float | None:
    row = conn.execute(
        """
        SELECT MAX(started_at) FROM repair_jobs
        WHERE station=? AND cause=?
        """,
        (int(station), str(cause)),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


__all__ = ["last_repair_at"]
