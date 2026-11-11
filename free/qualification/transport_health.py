"""Incremental ``transport_health`` aggregation.

The aggregate is recomputed from the append-only attempts table after each
qualified attempt.  One row is kept per station and one global row at station
``0``; ``degraded`` is set when a transport reaches the configured attempt
threshold without a single success.  ``server_transports`` also keeps the last
success and last error so hostname-level views stay truthful.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .domain import Attempt, AttemptStatus

INFRA_STATUSES = frozenset(
    {
        AttemptStatus.NO_EGRESS,
        AttemptStatus.BIND_ERROR,
        AttemptStatus.TIMEOUT,
        AttemptStatus.DUPLICATE,
    }
)


def _aggregate(
    conn: sqlite3.Connection,
    transport: str,
    *,
    station: int | None,
) -> dict[str, Any]:
    where = "" if station is None else " AND station = ?"
    params: tuple[Any, ...] = (transport,) if station is None else (transport, station)
    rows = conn.execute(
        f"""
        SELECT status, rtt_ms, latency_ms, error, finished_at
        FROM qualification_attempts
        WHERE transport = ?{where}
        ORDER BY finished_at
        """,
        params,
    ).fetchall()
    attempts_count = len(rows)
    successes = sum(1 for row in rows if row["status"] == AttemptStatus.OK)
    quota = sum(1 for row in rows if row["status"] == AttemptStatus.QUOTA)
    infra = sum(1 for row in rows if row["status"] in INFRA_STATUSES)
    latencies = sorted(
        float(row["rtt_ms"] or 0.0) or float(row["latency_ms"] or 0.0)
        for row in rows
        if row["status"] == AttemptStatus.OK
    )
    latencies = [value for value in latencies if value > 0]
    if latencies:
        middle = len(latencies) // 2
        latency_p50 = (
            latencies[middle]
            if len(latencies) % 2
            else (latencies[middle - 1] + latencies[middle]) / 2.0
        )
    else:
        latency_p50 = None
    last_error = ""
    for row in rows:
        text = str(row["error"] or "")
        if text:
            last_error = text
    if not last_error and successes == 0 and rows:
        last_error = str(rows[-1]["status"] or "")
    return {
        "attempts": attempts_count,
        "successes": successes,
        "quota": quota,
        "infra": infra,
        "latency_p50": latency_p50,
        "last_error": last_error[:500],
    }


def refresh_transport_health(
    conn: sqlite3.Connection,
    attempt: Attempt,
    *,
    min_attempts: int,
    now: float,
) -> None:
    """Recompute the station-local and global aggregate for one transport."""

    transport = str(attempt.transport or "")
    if not transport:
        return
    threshold = max(1, int(min_attempts))
    station = int(attempt.station or 0)
    for scope in sorted({0, station}):
        values = _aggregate(conn, transport, station=None if scope == 0 else scope)
        attempts_count = values["attempts"]
        successes = values["successes"]
        success_rate = successes / attempts_count if attempts_count else 0.0
        degraded = int(attempts_count >= threshold and successes == 0)
        conn.execute(
            """
            INSERT INTO transport_health(
                transport, station, attempts, successes, quota, infra,
                latency_p50_ms, success_rate, degraded, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transport, station) DO UPDATE SET
                attempts=excluded.attempts,
                successes=excluded.successes,
                quota=excluded.quota,
                infra=excluded.infra,
                latency_p50_ms=excluded.latency_p50_ms,
                success_rate=excluded.success_rate,
                degraded=excluded.degraded,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                transport,
                scope,
                attempts_count,
                successes,
                values["quota"],
                values["infra"],
                values["latency_p50"],
                success_rate,
                degraded,
                values["last_error"],
                now,
            ),
        )
    if attempt.status is AttemptStatus.OK:
        conn.execute(
            """
            UPDATE server_transports
            SET last_ok_at = ?, last_error = ''
            WHERE hostname = ? AND transport = ?
            """,
            (now, str(attempt.hostname), transport),
        )
    else:
        conn.execute(
            """
            UPDATE server_transports
            SET last_error = ?
            WHERE hostname = ? AND transport = ?
            """,
            (
                str(attempt.error or attempt.status)[:500],
                str(attempt.hostname),
                transport,
            ),
        )


__all__ = ["INFRA_STATUSES", "refresh_transport_health"]
