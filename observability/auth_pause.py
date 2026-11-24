"""Compteurs et pause AUTH uniques — feature 019, tâche T314.

Deux chaînes peuvent observer un ``AUTH_FAILED`` dans la même minute. Avant
cette table, chacune décidait d'armer la pause globale et réécrivait le même
fichier : selon l'ordre d'écriture, la pause pouvait être raccourcie ou
prolongée sans preuve. Désormais :

* les rejets sont écrits dans ``registry.db.auth_events`` (compteurs par
  station et fenêtre globale) ;
* la pause est une ligne ``auth_pauses`` créée sous ``BEGIN IMMEDIATE`` ;
* le premier écrivain gagne (``writer=True``), les suivants constatent la
  pause existante et ne réécrivent rien.

Le fichier ``logs/auth_global_pause.json`` reste le contrat lu par le pool :
seul le superviseur l'écrit, et seulement quand ``writer=True``.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "logs", "registry.db")

DEFAULT_WINDOW_S = 60.0
DEFAULT_THRESHOLD = 2
DEFAULT_PAUSE_S = 1800.0


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_schema(db_path: str = DEFAULT_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                station INTEGER NOT NULL DEFAULT 0,
                transport TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'rejection'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_events_at ON auth_events(at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_pauses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                until REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                rejections INTEGER NOT NULL DEFAULT 0,
                transport TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_pauses_until ON auth_pauses(until)"
        )
    finally:
        conn.close()


def note_rejection(
    db_path: str = DEFAULT_DB,
    *,
    station: int,
    transport: str = "",
    now: float | None = None,
    window_s: float = DEFAULT_WINDOW_S,
    threshold: int = DEFAULT_THRESHOLD,
    pause_s: float = DEFAULT_PAUSE_S,
) -> dict[str, Any]:
    """Enregistre un rejet et arme la pause au plus une fois.

    Retourne ``{"pause", "until", "writer", "count", "pause_id"}``. ``writer``
    est vrai uniquement pour l'appel qui a créé la ligne de pause : c'est le
    seul autorisé à réécrire ``auth_global_pause.json``.
    """

    ts = time.time() if now is None else float(now)
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO auth_events(at, station, transport, kind)"
                " VALUES (?,?,?,'rejection')",
                (ts, int(station or 0), str(transport or "")),
            )
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM auth_events"
                    " WHERE at>=? AND kind='rejection'",
                    (ts - max(1.0, float(window_s)),),
                ).fetchone()[0]
                or 0
            )
            active = conn.execute(
                "SELECT id, until FROM auth_pauses WHERE until>?"
                " ORDER BY until DESC LIMIT 1",
                (ts,),
            ).fetchone()
            if active is not None:
                conn.execute("COMMIT")
                return {
                    "pause": True,
                    "until": float(active["until"]),
                    "writer": False,
                    "count": count,
                    "pause_id": int(active["id"]),
                }
            if count < max(1, int(threshold)):
                conn.execute("COMMIT")
                return {
                    "pause": False,
                    "until": 0.0,
                    "writer": False,
                    "count": count,
                    "pause_id": 0,
                }
            until = ts + max(1.0, float(pause_s))
            cur = conn.execute(
                "INSERT INTO auth_pauses(created_at, until, reason,"
                " rejections, transport) VALUES (?,?,?,?,?)",
                (
                    ts,
                    until,
                    "auth-rejection-window",
                    count,
                    str(transport or ""),
                ),
            )
            conn.execute("COMMIT")
            return {
                "pause": True,
                "until": until,
                "writer": True,
                "count": count,
                "pause_id": int(cur.lastrowid or 0),
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def active_pause(db_path: str = DEFAULT_DB, *, now: float | None = None) -> float:
    """Fin de la pause active (0.0 si aucune)."""

    ts = time.time() if now is None else float(now)
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(until) FROM auth_pauses WHERE until>?", (ts,)
            ).fetchone()
            return float(row[0] or 0.0) if row else 0.0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0


def station_counts(
    db_path: str = DEFAULT_DB,
    *,
    now: float | None = None,
    window_s: float = 1800.0,
) -> dict[int, int]:
    """Rejets par station sur la fenêtre (pour /health et diagnostic)."""

    ts = time.time() if now is None else float(now)
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT station, COUNT(*) FROM auth_events"
                " WHERE at>=? AND kind='rejection' GROUP BY station",
                (ts - max(1.0, float(window_s)),),
            ).fetchall()
            return {int(r[0] or 0): int(r[1] or 0) for r in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
