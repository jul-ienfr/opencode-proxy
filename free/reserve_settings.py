"""Réglages runtime de la réserve client (022 T018).

La GUI ne peut pas écrire ``config.yaml`` : le runtime réécrit ce fichier et
perd les clés hors schéma (constaté le 2026-11-15). Les réglages vivent donc
dans ``registry.db``, partagés par l'app et le superviseur.

Précédence : variable d'environnement > réglage GUI > défaut. Une variable
d'environnement est un override de déploiement ; la GUI règle le cas courant.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "logs" / "registry.db"

DEFAULTS: dict[str, str] = {
    "enabled": "1",
    "target": "6",
    "min": "2",
    "ttl_s": "3600",
    "client_budget_pct": "80",
    "probe_budget_pct": "50",
}

ENV_NAMES: dict[str, str] = {
    "enabled": "CLIENT_RESERVE_ENABLED",
    "target": "RESERVE_TARGET",
    "min": "RESERVE_MIN",
    "ttl_s": "RESERVE_TTL_S",
    "client_budget_pct": "CLIENT_BUDGET_PCT",
    "probe_budget_pct": "PROBE_BUDGET_PCT",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_reserve_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_CACHE: tuple[float, dict[str, str]] | None = None
_CACHE_TTL_S = 2.0


def _connect(db: Path | None = None) -> sqlite3.Connection:
    return sqlite3.connect(str(db or DEFAULT_DB), timeout=5.0)


def _read_all(db: Path | None = None) -> dict[str, str]:
    try:
        with _connect(db) as conn:
            conn.executescript(_SCHEMA)
            rows = conn.execute(
                "SELECT key, value FROM client_reserve_settings"
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows if row and row[0]}
    except Exception:
        return {}


def all_settings(
    *, now: float | None = None, db: Path | None = None
) -> dict[str, str]:
    """Valeurs effectives (env > DB > défaut)."""

    global _CACHE
    ts = time.time() if now is None else float(now)
    stored: dict[str, str]
    if db is None and _CACHE is not None:
        cached_at, cached = _CACHE
        stored = cached if ts - cached_at < _CACHE_TTL_S else _read_all()
    else:
        stored = _read_all(db)
    out: dict[str, str] = {}
    for name, default in DEFAULTS.items():
        env = str(os.getenv(ENV_NAMES.get(name, ""), "") or "").strip()
        if env:
            out[name] = env
        else:
            out[name] = stored.get(name, default)
    if db is None:
        _CACHE = (ts, stored)
    return out


def get(name: str, default: str = "") -> str:
    return all_settings().get(name, default)


def get_int(name: str, default: int) -> int:
    try:
        return int(float(str(get(name, str(default))).strip()))
    except (TypeError, ValueError):
        return int(default)


def get_float(name: str, default: float) -> float:
    try:
        return float(str(get(name, str(default))).strip())
    except (TypeError, ValueError):
        return float(default)


def get_bool(name: str, default: bool = True) -> bool:
    value = str(get(name, "1" if default else "0")).strip().lower()
    return value not in ("0", "false", "no", "off")


def _validate(name: str, value: object) -> str:
    if name == "enabled":
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return "1"
        if text in ("0", "false", "no", "off"):
            return "0"
        raise ValueError("enabled doit être un booléen")
    if name == "target":
        number = int(float(str(value)))
        if not 1 <= number <= 6:
            raise ValueError("target doit être entre 1 et 6 (six stations)")
        return str(number)
    if name == "min":
        number = int(float(str(value)))
        if not 0 <= number <= 6:
            raise ValueError("min doit être entre 0 et 6")
        return str(number)
    if name == "ttl_s":
        number = float(str(value))
        if not 60.0 <= number <= 86400.0:
            raise ValueError("ttl_s doit être entre 60 et 86400")
        return str(int(number))
    if name in ("client_budget_pct", "probe_budget_pct"):
        number = float(str(value))
        if not 1.0 <= number <= 100.0:
            raise ValueError(f"{name} doit être entre 1 et 100")
        return str(int(number))
    raise ValueError(f"réglage inconnu: {name}")


def set_many(
    values: dict[str, object],
    *,
    now: float | None = None,
    db: Path | None = None,
) -> dict[str, str]:
    """Valide puis persiste les réglages ; retourne les valeurs effectives."""

    global _CACHE
    ts = time.time() if now is None else float(now)
    cleaned = {
        name: _validate(name, value)
        for name, value in values.items()
        if name in DEFAULTS
    }
    if not cleaned:
        return all_settings(now=ts, db=db)
    with _connect(db) as conn:
        conn.executescript(_SCHEMA)
        for name, value in cleaned.items():
            conn.execute(
                "INSERT INTO client_reserve_settings(key, value, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (name, value, ts),
            )
        conn.commit()
    _CACHE = None
    return all_settings(now=ts, db=db)
