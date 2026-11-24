"""Pins de stations — feature 019, tâche T306.

Le registre est la seule autorité des pins. ``.env`` en est un rendu généré
(``SERVER_HOSTNAMES_STATIONn`` / ``SERVER_COUNTRIES_STATIONn``), jamais édité à
la main. Deux cycles de pin consécutifs ne produisent qu'une écriture par
changement et aucun pin orphelin (station absente ou > 6).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Mapping

__all__ = [
    "MAX_STATION",
    "clear_pin",
    "migrate_from_env",
    "read_env_pins",
    "ensure_schema",
    "env_updates",
    "load_pins",
    "prune_orphans",
    "set_pin",
    "sync_env",
]

MAX_STATION = 6
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "logs", "registry.db")
DEFAULT_ENV = os.path.join(ROOT, ".env")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    station     INTEGER PRIMARY KEY,
    hostname    TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT '',
    egress_ip   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL
)
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def set_pin(
    db_path: str,
    station: int,
    hostname: str,
    *,
    country: str = "",
    egress_ip: str = "",
    source: str = "supervisor",
    now: float | None = None,
) -> bool:
    """Upsert idempotent. Retourne True si la ligne a changé."""
    sid = int(station)
    if not (1 <= sid <= MAX_STATION) or not hostname.strip():
        return False
    ts = float(now if now is not None else time.time())
    try:
        conn = _connect(db_path)
        try:
            before = conn.execute(
                "SELECT hostname, country FROM pins WHERE station=?", (sid,)
            ).fetchone()
            if before and str(before[0]) == hostname.strip() and str(before[1]) == country.strip():
                return False
            conn.execute(
                """
                INSERT INTO pins(station, hostname, country, egress_ip, source, updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(station) DO UPDATE SET
                    hostname=excluded.hostname,
                    country=excluded.country,
                    egress_ip=excluded.egress_ip,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (sid, hostname.strip(), country.strip(), egress_ip.strip(), source, ts),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def clear_pin(db_path: str, station: int) -> bool:
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute("DELETE FROM pins WHERE station=?", (int(station),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def load_pins(db_path: str = DEFAULT_DB) -> dict[int, dict[str, str]]:
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT station, hostname, country, egress_ip, source, updated_at FROM pins"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {
        int(r[0]): {
            "hostname": str(r[1] or ""),
            "country": str(r[2] or ""),
            "egress_ip": str(r[3] or ""),
            "source": str(r[4] or ""),
            "updated_at": float(r[5] or 0.0),
        }
        for r in rows
    }


def prune_orphans(db_path: str = DEFAULT_DB, *, max_station: int = MAX_STATION) -> list[int]:
    """Supprime les pins hors flotte (7..10 historiques). Retourne les ids."""
    try:
        conn = _connect(db_path)
        try:
            bad = [
                int(r[0])
                for r in conn.execute(
                    "SELECT station FROM pins WHERE station<? OR station>?",
                    (1, int(max_station)),
                ).fetchall()
            ]
            if bad:
                conn.executemany(
                    "DELETE FROM pins WHERE station=?",
                    [(sid,) for sid in bad],
                )
                conn.commit()
            return bad
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def env_updates(pins: Mapping[int, Mapping[str, str]]) -> dict[str, str]:
    """Rendu complet des clés de pin pour les stations 1..6 (vides incluses)."""
    out: dict[str, str] = {}
    for sid in range(1, MAX_STATION + 1):
        pin = pins.get(sid) or {}
        out[f"SERVER_HOSTNAMES_STATION{sid}"] = str(pin.get("hostname") or "")
        out[f"SERVER_COUNTRIES_STATION{sid}"] = str(pin.get("country") or "")
    return out


def sync_env(
    db_path: str = DEFAULT_DB,
    env_path: str | Path = DEFAULT_ENV,
    *,
    pins: Mapping[int, Mapping[str, str]] | None = None,
) -> dict[str, str]:
    """Écrit ``.env`` depuis le registre, en une seule transaction de fichier."""
    current = dict(pins) if pins is not None else load_pins(db_path)
    updates = env_updates(current)
    try:
        from ops.env_store import update_env

        update_env(env_path, updates)
    except Exception:
        return {}
    return updates


def read_env_pins(env_path: str | Path = DEFAULT_ENV) -> dict[int, dict[str, str]]:
    """Lit les pins historiques du ``.env`` (migration T306)."""
    out: dict[int, dict[str, str]] = {}
    try:
        lines = Path(env_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    for sid in range(1, MAX_STATION + 1):
        host = values.get(f"SERVER_HOSTNAMES_STATION{sid}", "").strip()
        country = values.get(f"SERVER_COUNTRIES_STATION{sid}", "").strip()
        if host or country:
            out[sid] = {"hostname": host, "country": country}
    return out


def migrate_from_env(
    db_path: str = DEFAULT_DB,
    env_path: str | Path = DEFAULT_ENV,
    *,
    now: float | None = None,
) -> int:
    """Importe les pins ``.env`` absents du registre (une fois, idempotent)."""
    legacy = read_env_pins(env_path)
    if not legacy:
        return 0
    existing = load_pins(db_path)
    imported = 0
    for sid, pin in legacy.items():
        if sid in existing:
            continue
        if set_pin(
            db_path,
            sid,
            pin.get("hostname", ""),
            country=pin.get("country", ""),
            source="env-migration",
            now=now,
        ):
            imported += 1
    return imported
