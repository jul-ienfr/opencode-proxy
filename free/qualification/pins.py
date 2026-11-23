"""Autorité unique des pins hostname (019 T306).

Le registre ``registry.db`` porte le pin ; ``.env`` n'en est qu'un **export**.
Un seul écrivain (le superviseur de chaîne) appelle :func:`upsert_pin` ; la
migration depuis ``.env`` est idempotente et ne réécrit que les différences.

Invariants :

* une station = au plus un pin (clé primaire ``station``) ;
* un hostname vide supprime le pin (retour au choix libre de gluetun) ;
* ``source`` distingue ``env-migration``, ``app`` et ``operator`` pour
  expliquer une éventuelle divergence entre deux cycles.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass

from free.sql_schemas import PINS_SCHEMA


@dataclass(frozen=True, slots=True)
class Pin:
    station: int
    provider: str
    hostname: str
    country: str
    egress_ip: str
    transport: str
    source: str
    pinned_at: float
    updated_at: float


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée la table ``pins`` (idempotent, aucun scan)."""
    conn.executescript(PINS_SCHEMA)


def _pin_from_row(row: sqlite3.Row | tuple) -> Pin:
    return Pin(
        station=int(row[0]),
        provider=str(row[1] or ""),
        hostname=str(row[2] or ""),
        country=str(row[3] or ""),
        egress_ip=str(row[4] or ""),
        transport=str(row[5] or ""),
        source=str(row[6] or ""),
        pinned_at=float(row[7] or 0.0),
        updated_at=float(row[8] or 0.0),
    )


def upsert_pin(
    conn: sqlite3.Connection,
    station: int,
    hostname: str,
    *,
    provider: str = "nordvpn",
    country: str = "",
    egress_ip: str = "",
    transport: str = "",
    source: str = "app",
    now: float | None = None,
) -> Pin | None:
    """Écrit le pin d'une station. Un hostname vide supprime le pin.

    Retourne le pin écrit, ou ``None`` si le pin a été supprimé.
    """
    sid = int(station)
    host = str(hostname or "").strip()
    if not host:
        delete_pin(conn, sid)
        return None
    ts = time.time() if now is None else float(now)
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO pins(station, provider, hostname, country, egress_ip,
                         transport, source, pinned_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station) DO UPDATE SET
            provider=excluded.provider,
            hostname=excluded.hostname,
            country=excluded.country,
            egress_ip=excluded.egress_ip,
            transport=excluded.transport,
            source=excluded.source,
            pinned_at=CASE
                WHEN pins.hostname = excluded.hostname THEN pins.pinned_at
                ELSE excluded.pinned_at
            END,
            updated_at=excluded.updated_at
        """,
        (
            sid,
            str(provider or "nordvpn"),
            host,
            str(country or ""),
            str(egress_ip or ""),
            str(transport or ""),
            str(source or "app"),
            ts,
            ts,
        ),
    )
    row = get_pin(conn, sid)
    return row


def delete_pin(conn: sqlite3.Connection, station: int) -> bool:
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM pins WHERE station=?", (int(station),))
    return bool(cur.rowcount)


def get_pin(conn: sqlite3.Connection, station: int) -> Pin | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT station, provider, hostname, country, egress_ip, transport,"
        " source, pinned_at, updated_at FROM pins WHERE station=?",
        (int(station),),
    ).fetchone()
    return _pin_from_row(row) if row is not None else None


def list_pins(conn: sqlite3.Connection) -> list[Pin]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT station, provider, hostname, country, egress_ip, transport,"
        " source, pinned_at, updated_at FROM pins ORDER BY station"
    ).fetchall()
    return [_pin_from_row(row) for row in rows]


def env_hostnames(conn: sqlite3.Connection) -> dict[str, str]:
    """Export ``.env`` : ``{"SERVER_HOSTNAMES_STATION1": "host", ...}``."""
    return {
        f"SERVER_HOSTNAMES_STATION{pin.station}": pin.hostname
        for pin in list_pins(conn)
        if pin.hostname
    }


def migrate_env(conn: sqlite3.Connection, environ: Mapping[str, str], *, now: float | None = None) -> int:
    """Importe les pins ``SERVER_HOSTNAMES_STATIONn`` d'un environnement.

    Retourne le nombre de pins **modifiés** (un second appel avec le même
    environnement retourne 0 : la migration est idempotente).
    """
    ensure_schema(conn)
    changed = 0
    for key, value in environ.items():
        if not key.startswith("SERVER_HOSTNAMES_STATION"):
            continue
        suffix = key[len("SERVER_HOSTNAMES_STATION") :]
        if not suffix.isdigit():
            continue
        sid = int(suffix)
        host = str(value or "").strip()
        current = get_pin(conn, sid)
        if current is not None and current.hostname == host:
            continue
        if not host and current is None:
            continue
        upsert_pin(conn, sid, host, source="env-migration", now=now)
        changed += 1
    return changed


def backfill_egress_ip(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    """Remplit ``egress_ip`` depuis ``station_state.current_ip`` quand vide.

    Ne touche qu'aux pins dont l'IP de sortie est vide et pour lesquels
    ``station_state`` porte une IP courante non vide. Fail-soft si la table
    ``station_state`` est absente (retourne 0). Retourne le nombre de lignes
    mises à jour.
    """
    ensure_schema(conn)
    ts = time.time() if now is None else float(now)
    try:
        cur = conn.execute(
            """
            UPDATE pins
            SET egress_ip = (
                    SELECT ss.current_ip
                    FROM station_state ss
                    WHERE ss.station = pins.station AND ss.current_ip <> ''
                ),
                updated_at = ?
            WHERE (pins.egress_ip IS NULL OR pins.egress_ip = '')
              AND EXISTS (
                    SELECT 1
                    FROM station_state ss
                    WHERE ss.station = pins.station AND ss.current_ip <> ''
              )
            """,
            (ts,),
        )
    except sqlite3.OperationalError:
        return 0
    return int(cur.rowcount or 0)


__all__ = [
    "Pin",
    "backfill_egress_ip",
    "delete_pin",
    "ensure_schema",
    "env_hostnames",
    "get_pin",
    "list_pins",
    "migrate_env",
    "upsert_pin",
]
