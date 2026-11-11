"""Fleet incident journal backed by ``registry.db``.

The station SLO sampler already tracks per-station availability. This module
adds the fleet-level alerts the allocator must surface: low usable count, cold
warm pool, degraded capacity, duplicate egress and a stalled sweep. Incidents
are deduplicated by ``(scope, cause)`` and closed automatically when the
condition disappears, so the journal is a state view rather than an
append-only log.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "logs" / "registry.db"


def _connect(path: str | Path | None = None):
    from rotation.infrastructure.db.migrate import connect

    return connect(Path(path) if path else DEFAULT_DB)


def _key(scope: str, station: int | None, cause: str) -> tuple[str, int, str]:
    try:
        sid = int(station or 0)
    except (TypeError, ValueError):
        sid = 0
    return (str(scope), sid, str(cause))


def sync_incidents(
    alerts: Iterable[dict[str, Any]],
    *,
    path: str | Path | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Open/close fleet incidents so the open set equals ``alerts``."""

    ts = time.time() if now is None else float(now)
    wanted: dict[tuple[str, int, str], dict[str, Any]] = {}
    for alert in alerts:
        cause = str(alert.get("cause") or "").strip()
        scope = str(alert.get("scope") or "fleet").strip()
        if not cause:
            continue
        wanted[_key(scope, alert.get("station"), cause)] = alert
    conn = _connect(path)
    opened = closed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, scope, COALESCE(station,0) AS station, cause "
            "FROM incidents WHERE ended_at IS NULL"
        ).fetchall()
        open_keys = {
            _key(str(row["scope"]), row["station"], str(row["cause"]))
            for row in rows
        }
        for row in rows:
            key = _key(str(row["scope"]), row["station"], str(row["cause"]))
            if key not in wanted:
                conn.execute(
                    "UPDATE incidents SET ended_at=?, remediation=COALESCE("
                    "remediation,'') || ' auto-cleared' WHERE id=?",
                    (ts, int(row["id"])),
                )
                closed += 1
        for key, alert in wanted.items():
            if key in open_keys:
                continue
            conn.execute(
                "INSERT INTO incidents(scope, station, started_at, cause, "
                "remediation, details) VALUES(?,?,?,?,?,?)",
                (
                    key[0],
                    key[1] or None,
                    ts,
                    key[2],
                    str(alert.get("remediation") or ""),
                    json.dumps(alert.get("details") or {}, separators=(",", ":")),
                ),
            )
            opened += 1
        conn.commit()
        return {"opened": opened, "closed": closed, "open": len(wanted)}
    except Exception:
        conn.rollback()
        return {"opened": 0, "closed": 0, "open": 0}
    finally:
        conn.close()


def open_incidents(
    *, path: str | Path | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    target = Path(path) if path else DEFAULT_DB
    if not target.exists():
        return []
    conn = _connect(target)
    try:
        rows = conn.execute(
            "SELECT id, scope, station, started_at, cause, remediation, details "
            "FROM incidents WHERE ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


__all__ = ["DEFAULT_DB", "open_incidents", "sync_incidents"]
