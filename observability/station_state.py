"""Autorité de l'état station — feature 019, tâches T305 et T308.

Une seule vérité pour ``(station, client_usable, current_ip)`` : l'app écrit
``registry.db/station_state`` à chaque probe ; l'API lit cette table au lieu de
recalculer ; le fork reste l'autorité de l'IP, recopiée ici.

``STATION_STATE_SOURCE`` règle la période de transition :

* ``shadow`` (défaut) — l'app écrit, la qualification n'écrit plus mais
  journalise les divergences. À garder 24 h.
* ``app`` — l'app seule écrit (cible après la fenêtre de shadow, identique).
* ``qualification`` — repli : la qualification reste seule écrivain.

Le module est volontairement sans dépendance projet au chargement (SQLite nu)
pour rester utilisable depuis la sonde et les tests.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Iterable, Mapping

__all__ = [
    "AUTHORITIES",
    "authority",
    "detect_divergences",
    "ensure_schema",
    "overlay",
    "qualification_may_write",
    "read_all",
    "record",
]

_LOG = logging.getLogger(__name__)
_SOURCE_ENV = "STATION_STATE_SOURCE"
AUTHORITIES = ("app", "shadow", "qualification")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "logs", "registry.db")
OVERLAY_MAX_AGE_S = 180.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_state (
    station             INTEGER PRIMARY KEY,
    container           TEXT NOT NULL DEFAULT '',
    transport_effective TEXT NOT NULL DEFAULT '',
    state               TEXT NOT NULL DEFAULT '',
    country             TEXT NOT NULL DEFAULT '',
    current_ip          TEXT NOT NULL DEFAULT '',
    current_host        TEXT NOT NULL DEFAULT '',
    client_usable       INTEGER NOT NULL DEFAULT 0,
    last_probe_at       REAL NOT NULL DEFAULT 0,
    last_200_at         REAL,
    reason              TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL
)
"""


def authority() -> str:
    value = os.getenv(_SOURCE_ENV, "shadow").strip().lower()
    return value if value in AUTHORITIES else "shadow"


def qualification_may_write() -> bool:
    """``shadow``/``app`` : la qualification n'écrit plus (seulement ``qualification``)."""
    return authority() == "qualification"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def record(
    db_path: str,
    station: int,
    *,
    state: str,
    current_ip: str = "",
    current_host: str = "",
    container: str = "",
    transport: str = "",
    country: str = "",
    client_usable: bool = False,
    last_probe_at: float | None = None,
    last_200_at: float | None = None,
    reason: str = "",
    now: float | None = None,
) -> bool:
    """Upsert résilient (jamais d'exception dans le chemin de probe)."""
    ts = float(now if now is not None else time.time())
    try:
        conn = _connect(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO station_state(
                    station, container, transport_effective, state, country,
                    current_ip, current_host, client_usable, last_probe_at,
                    last_200_at, reason, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(station) DO UPDATE SET
                    container=excluded.container,
                    transport_effective=excluded.transport_effective,
                    state=excluded.state,
                    country=excluded.country,
                    current_ip=excluded.current_ip,
                    current_host=excluded.current_host,
                    client_usable=excluded.client_usable,
                    last_probe_at=excluded.last_probe_at,
                    last_200_at=COALESCE(excluded.last_200_at, station_state.last_200_at),
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    int(station),
                    str(container or ""),
                    str(transport or ""),
                    str(state or ""),
                    str(country or ""),
                    str(current_ip or ""),
                    str(current_host or ""),
                    1 if client_usable else 0,
                    float(last_probe_at if last_probe_at is not None else ts),
                    None if last_200_at is None else float(last_200_at),
                    str(reason or ""),
                    ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error as exc:
        _LOG.debug("[station-state] write skipped: %s", exc)
        return False


def read_all(db_path: str = DEFAULT_DB) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            cur = conn.execute("SELECT * FROM station_state")
            names = [d[0] for d in cur.description or ()]
            for raw in cur.fetchall():
                item = dict(zip(names, raw))
                rows[int(item["station"])] = item
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return rows


def overlay(
    stations: Iterable[Mapping[str, Any]],
    rows: Mapping[int, Mapping[str, Any]],
    *,
    now: float | None = None,
    max_age_s: float = OVERLAY_MAX_AGE_S,
) -> list[dict[str, Any]]:
    """Applique l'état table sur les lignes API. Fonction pure (T308).

    Une ligne plus vieille que ``max_age_s`` n'est pas appliquée : l'API ne
    doit pas servir un état périmé comme s'il était vivant.
    """
    ts = float(now if now is not None else time.time())
    out: list[dict[str, Any]] = []
    for entry in stations:
        merged = dict(entry)
        station = merged.get("station")
        row = rows.get(int(station)) if station is not None else None
        if row is not None:
            updated = float(row.get("updated_at") or 0.0)
            fresh = updated > 0 and ts - updated <= float(max_age_s)
            merged["state_authority"] = "app"
            merged["state_age_s"] = max(0.0, ts - updated) if updated else None
            if fresh:
                merged["client_usable"] = bool(row.get("client_usable"))
                merged["station_usable"] = bool(row.get("client_usable"))
                merged["state_reason"] = str(row.get("reason") or "")
                merged["last_200_at"] = row.get("last_200_at")
                if row.get("current_ip"):
                    merged["current_ip"] = str(row["current_ip"])
            else:
                merged["state_stale"] = True
        out.append(merged)
    return out


def detect_divergences(
    api_rows: Iterable[Mapping[str, Any]],
    table_rows: Mapping[int, Mapping[str, Any]],
    fork_rows: Mapping[int, Mapping[str, Any]],
) -> list[str]:
    """Retourne la liste des divergences ``(station, client_usable, ip)``.

    Utilisé par le test de non-divergence T308 et par le mode shadow.
    """
    problems: list[str] = []
    api = {int(r["station"]): r for r in api_rows if r.get("station") is not None}
    for station in sorted(set(api) | set(table_rows) | set(fork_rows)):
        api_row = api.get(station)
        table_row = table_rows.get(station)
        fork_row = fork_rows.get(station)
        if api_row is None or table_row is None:
            problems.append(f"s{station}: missing api/table row")
            continue
        api_usable = bool(api_row.get("client_usable"))
        if api_usable != bool(table_row.get("client_usable")):
            problems.append(
                f"s{station}: client_usable api={api_usable} table={bool(table_row.get('client_usable'))}"
            )
        if fork_row is not None:
            fork_ip = str(fork_row.get("current_ip") or "")
            for label, candidate in (
                ("api", str(api_row.get("current_ip") or "")),
                ("table", str(table_row.get("current_ip") or "")),
            ):
                if fork_ip and candidate and candidate != fork_ip:
                    problems.append(f"s{station}: ip {label}={candidate} fork={fork_ip}")
    return problems
