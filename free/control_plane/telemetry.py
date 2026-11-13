"""SQLite sink for control plane shadow decisions.

The table is additive and independent from ``registry.db``: shadow observations
never create an IP claim or a lease.  Each row is one legacy decision compared
with the fork's dry-run plan and the IP actually observed after the cut.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "logs" / "telemetry.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_plane_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    station TEXT NOT NULL DEFAULT '',
    legacy_target TEXT NOT NULL DEFAULT '',
    shadow_target TEXT NOT NULL DEFAULT '',
    legacy_ip TEXT NOT NULL DEFAULT '',
    shadow_ip TEXT NOT NULL DEFAULT '',
    match INTEGER NOT NULL DEFAULT 0,
    divergence_reason TEXT NOT NULL DEFAULT '',
    would_cut_tunnel INTEGER NOT NULL DEFAULT 0,
    warnings TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT ''
)
"""

_TRANSPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_plane_transport_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    station TEXT NOT NULL DEFAULT '',
    legacy_transport TEXT NOT NULL DEFAULT '',
    fork_transport TEXT NOT NULL DEFAULT '',
    match INTEGER NOT NULL DEFAULT 0,
    legacy_reason TEXT NOT NULL DEFAULT '',
    fork_reason TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
)
"""


class ShadowTelemetry:
    """Append-only ``control_plane_shadow`` table."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_PATH

    @property
    def path(self) -> Path:
        return self._path

    def record(self, entry: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                """
                INSERT INTO control_plane_shadow (
                    ts, station, legacy_target, shadow_target,
                    legacy_ip, shadow_ip, match, divergence_reason,
                    would_cut_tunnel, warnings, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(entry.get("ts", 0.0)),
                    str(entry.get("station", "")),
                    str(entry.get("legacy_target", "")),
                    str(entry.get("shadow_target", "")),
                    str(entry.get("legacy_ip", "")),
                    str(entry.get("shadow_ip", "")),
                    1 if entry.get("match") else 0,
                    str(entry.get("divergence_reason", "")),
                    1 if entry.get("would_cut_tunnel") else 0,
                    json.dumps(list(entry.get("warnings") or [])),
                    str(entry.get("error", "")),
                ),
            )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        with sqlite3.connect(self._path) as connection:
            connection.execute(_SCHEMA)
            rows = connection.execute(
                """
                SELECT ts, station, legacy_target, shadow_target,
                       legacy_ip, shadow_ip, match, divergence_reason,
                       would_cut_tunnel, warnings, error
                FROM control_plane_shadow
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "ts": row[0],
                "station": row[1],
                "legacy_target": row[2],
                "shadow_target": row[3],
                "legacy_ip": row[4],
                "shadow_ip": row[5],
                "match": bool(row[6]),
                "divergence_reason": row[7],
                "would_cut_tunnel": bool(row[8]),
                "warnings": json.loads(row[9] or "[]"),
                "error": row[10],
            }
            for row in rows
        ]

    def record_transport(self, entry: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(_TRANSPORT_SCHEMA)
            connection.execute(
                """
                INSERT INTO control_plane_transport_shadow (
                    ts, station, legacy_transport, fork_transport,
                    match, legacy_reason, fork_reason, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(entry.get("ts", 0.0)),
                    str(entry.get("station", "")),
                    str(entry.get("legacy_transport", "")),
                    str(entry.get("fork_transport", "")),
                    1 if entry.get("match") else 0,
                    str(entry.get("legacy_reason", "")),
                    str(entry.get("fork_reason", "")),
                    str(entry.get("error", "")),
                ),
            )

    def recent_transport(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        with sqlite3.connect(self._path) as connection:
            connection.execute(_TRANSPORT_SCHEMA)
            rows = connection.execute(
                """
                SELECT ts, station, legacy_transport, fork_transport,
                       match, legacy_reason, fork_reason, error
                FROM control_plane_transport_shadow
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "ts": row[0],
                "station": row[1],
                "legacy_transport": row[2],
                "fork_transport": row[3],
                "match": bool(row[4]),
                "legacy_reason": row[5],
                "fork_reason": row[6],
                "error": row[7],
            }
            for row in rows
        ]


__all__ = ["DEFAULT_PATH", "ShadowTelemetry"]
