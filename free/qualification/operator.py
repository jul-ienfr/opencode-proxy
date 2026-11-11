"""Operator pause/resume for one qualification candidate."""

from __future__ import annotations

import sqlite3


def set_paused_row(
    conn: sqlite3.Connection,
    hostname: str,
    *,
    paused: bool,
    now: float,
) -> bool:
    """Toggle ``operator-paused`` independently of the sweep."""

    if paused:
        cur = conn.execute(
            """
            UPDATE qualification_candidates
            SET status='skipped', skipped_reason='operator-paused',
                lease_until=0, next_eligible_at=0, updated_at=?
            WHERE hostname=? AND status IN ('pending','quota','claimed')
            """,
            (now, str(hostname)),
        )
    else:
        cur = conn.execute(
            """
            UPDATE qualification_candidates
            SET status='pending', skipped_reason='', lease_until=0,
                next_eligible_at=0, updated_at=?
            WHERE hostname=? AND status='skipped'
              AND skipped_reason='operator-paused'
            """,
            (now, str(hostname)),
        )
    return bool(cur.rowcount)


__all__ = ["set_paused_row"]
