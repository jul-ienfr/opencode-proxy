"""Échantillons SLO à la minute (019 T302/T313).

Une ligne par fenêtre dans ``registry.db`` (table ``slo_samples``). Le job
minute écrit les 9 indicateurs du plan de mesure ; la lecture permet de
prouver « 6/6 sur le même cycle » sans dépendre du dashboard.

Les valeurs sont **best-effort** : une table absente vaut 0, jamais une
exception (un échantillon manquant ne doit pas tuer le job).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from free.sql_schemas import SLO_SAMPLES_SCHEMA


_COLUMNS = (
    "sampled_at",
    "stations_client_usable",
    "warm_pool_live",
    "ip_200_rate",
    "rotate_latency_seconds",
    "app_up",
    "code_reload_total",
    "auth_attempts_total",
    "incidents_open",
    "rotation_jobs_orphans",
)


@dataclass(frozen=True, slots=True)
class SloSample:
    sampled_at: float
    stations_client_usable: int
    warm_pool_live: int
    ip_200_rate: float
    rotate_latency_seconds: float | None
    app_up: int
    code_reload_total: int
    auth_attempts_total: int
    incidents_open: int
    rotation_jobs_orphans: int


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SLO_SAMPLES_SCHEMA)


def record_sample(
    conn: sqlite3.Connection,
    *,
    sampled_at: float | None = None,
    stations_client_usable: int = 0,
    warm_pool_live: int = 0,
    ip_200_rate: float = 0.0,
    rotate_latency_seconds: float | None = None,
    app_up: int = 1,
    code_reload_total: int = 0,
    auth_attempts_total: int = 0,
    incidents_open: int = 0,
    rotation_jobs_orphans: int = 0,
) -> float:
    """Écrit une ligne SLO (upsert sur ``sampled_at``) et retourne son horodatage."""
    ts = time.time() if sampled_at is None else float(sampled_at)
    ensure_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO slo_samples
            (sampled_at, stations_client_usable, warm_pool_live, ip_200_rate,
             rotate_latency_seconds, app_up, code_reload_total,
             auth_attempts_total, incidents_open, rotation_jobs_orphans)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            int(stations_client_usable),
            int(warm_pool_live),
            float(ip_200_rate),
            None if rotate_latency_seconds is None else float(rotate_latency_seconds),
            int(app_up),
            int(code_reload_total),
            int(auth_attempts_total),
            int(incidents_open),
            int(rotation_jobs_orphans),
        ),
    )
    return ts


def _sample_from_row(row: sqlite3.Row | tuple) -> SloSample:
    return SloSample(
        sampled_at=float(row[0]),
        stations_client_usable=int(row[1]),
        warm_pool_live=int(row[2]),
        ip_200_rate=float(row[3]),
        rotate_latency_seconds=None if row[4] is None else float(row[4]),
        app_up=int(row[5]),
        code_reload_total=int(row[6]),
        auth_attempts_total=int(row[7]),
        incidents_open=int(row[8]),
        rotation_jobs_orphans=int(row[9]),
    )


def latest_samples(conn: sqlite3.Connection, *, limit: int = 60) -> list[SloSample]:
    ensure_schema(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM slo_samples"
        " ORDER BY sampled_at DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [_sample_from_row(row) for row in rows]


def six_of_six_held(
    conn: sqlite3.Connection, *, window_s: float = 600.0, now: float | None = None
) -> bool:
    """Vrai si **tous** les échantillons de la fenêtre portent 6/6.

    Une fenêtre vide est fausse : sans mesure, on ne prouve rien.
    """
    ts = time.time() if now is None else float(now)
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT stations_client_usable FROM slo_samples"
        " WHERE sampled_at >= ? ORDER BY sampled_at DESC",
        (ts - max(1.0, float(window_s)),),
    ).fetchall()
    return bool(rows) and all(int(row[0]) >= 6 for row in rows)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int | float | None:
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None
    except sqlite3.Error:
        return None


def sample_once(conn: sqlite3.Connection, *, now: float | None = None) -> SloSample:
    """Collecte les indicateurs disponibles dans ``registry.db``.

    Les compteurs qui vivent en mémoire dans l'app (``code_reload_total``,
    ``auth_attempts_total``) restent à 0 : T302 les expose via ``/metrics``
    et un collecteur externe peut les passer explicitement à
    :func:`record_sample`.
    """
    ts = time.time() if now is None else float(now)
    usable = int(_scalar(conn, "SELECT COUNT(*) FROM station_state WHERE client_usable=1") or 0)
    warm = int(_scalar(conn, "SELECT COUNT(*) FROM warm_pool WHERE expires_at > ?", (ts,)) or 0)
    incidents = int(_scalar(conn, "SELECT COUNT(*) FROM incidents WHERE ended_at IS NULL") or 0)
    try:
        attempts = int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM qualification_attempts"
                " WHERE finished_at >= ?",
                (ts - 900.0,),
            )
            or 0
        )
        successes = int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM qualification_attempts"
                " WHERE finished_at >= ? AND status IN ('ok','usable','success')",
                (ts - 900.0,),
            )
            or 0
        )
    except sqlite3.Error:
        attempts = successes = 0
    rate = (successes / attempts) if attempts else 0.0
    latency = _scalar(
        conn,
        "SELECT AVG(duration_ms) / 1000.0 FROM station_cutovers"
        " WHERE finished_at >= ? AND duration_ms IS NOT NULL",
        (ts - 900.0,),
    )
    cutovers_open = int(_scalar(conn, "SELECT COUNT(*) FROM rotation_jobs WHERE result='started'") or 0)
    record_sample(
        conn,
        sampled_at=ts,
        stations_client_usable=usable,
        warm_pool_live=warm,
        ip_200_rate=rate,
        rotate_latency_seconds=None if latency is None else float(latency),
        incidents_open=incidents,
        rotation_jobs_orphans=cutovers_open,
    )
    return SloSample(
        sampled_at=ts,
        stations_client_usable=usable,
        warm_pool_live=warm,
        ip_200_rate=rate,
        rotate_latency_seconds=None if latency is None else float(latency),
        app_up=1,
        code_reload_total=0,
        auth_attempts_total=0,
        incidents_open=incidents,
        rotation_jobs_orphans=cutovers_open,
    )


__all__ = [
    "SloSample",
    "ensure_schema",
    "latest_samples",
    "record_sample",
    "sample_once",
    "six_of_six_held",
]
