"""Synchronise les pins hostname entre ``registry.db`` et ``.env`` (019 T306).

Usages :

    python scripts/pins_sync.py import   # .env -> registry.db
    python scripts/pins_sync.py export   # registry.db -> lignes .env (stdout)
    python scripts/pins_sync.py export --write  # réécrit .env selon le registre
    python scripts/pins_sync.py fill-ip  # remplit egress_ip depuis station_state
    python scripts/pins_sync.py show     # pins du registre

Sans ``--write``, l'export imprime les lignes à recopier pour qu'un opérateur
garde le contrôle du fichier de secrets.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from free.qualification import pins as _pins  # noqa: E402


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


_STATION_HOST_PREFIX = "SERVER_HOSTNAMES_STATION"
_STATION_COUNTRY_PREFIX = "SERVER_COUNTRIES_STATION"


def _station_key(prefix: str, key: str) -> int | None:
    if not key.startswith(prefix):
        return None
    suffix = key[len(prefix) :]
    return int(suffix) if suffix.isdigit() else None


def env_station_lines(conn: sqlite3.Connection) -> dict[str, str]:
    """Lignes .env exportées depuis le registre (hostnames + countries)."""
    out: dict[str, str] = {}
    for pin in _pins.list_pins(conn):
        sid = int(pin.station)
        host = str(pin.hostname or "").strip()
        country = str(pin.country or "").strip()
        if host:
            out[f"{_STATION_HOST_PREFIX}{sid}"] = host
        if country:
            out[f"{_STATION_COUNTRY_PREFIX}{sid}"] = country
    return out


def render_env_from_registry(
    env_text: str, station_lines: dict[str, str]
) -> str:
    """Réécrit un .env en remplaçant/supprimant les clés station du registre.

    Les lignes ``SERVER_HOSTNAMES_STATIONn`` et ``SERVER_COUNTRIES_STATIONn``
    sont supprimées quand absentes de ``station_lines`` ; présentes, elles sont
    mises à jour. Les clés de ``station_lines`` manquantes du fichier sont
    ajoutées en fin de fichier.
    """
    wanted = dict(station_lines)
    out_lines: list[str] = []
    seen: set[str] = set()
    for raw in env_text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in raw:
            key = raw.split("=", 1)[0].strip()
            if (
                _station_key(_STATION_HOST_PREFIX, key) is not None
                or _station_key(_STATION_COUNTRY_PREFIX, key) is not None
            ):
                if key in wanted:
                    out_lines.append(f"{key}={wanted[key]}")
                    seen.add(key)
                # absent du registre => supprimé
                continue
        out_lines.append(raw)
    for key in sorted(wanted):
        if key not in seen:
            out_lines.append(f"{key}={wanted[key]}")
    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def write_env_from_registry(env_path: Path, conn: sqlite3.Connection) -> str:
    """Réécrit ``env_path`` selon le registre. Retourne le nouveau contenu."""
    current = env_path.read_text(encoding="utf-8", errors="replace") if env_path.is_file() else ""
    rendered = render_env_from_registry(current, env_station_lines(conn))
    env_path.write_text(rendered, encoding="utf-8")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "export", "show", "fill-ip"))
    parser.add_argument("--db", default=str(ROOT / "logs" / "registry.db"))
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument(
        "--write",
        action="store_true",
        help="export: réécrit .env selon le registre (sinon stdout)",
    )
    args = parser.parse_args(argv)

    conn = _connect(Path(args.db))
    try:
        if args.action == "import":
            changed = _pins.migrate_env(conn, _read_env(Path(args.env)))
            conn.commit()
            print(f"pins importés: {changed} modification(s)")
            return 0
        if args.action == "fill-ip":
            filled = _pins.backfill_egress_ip(conn)
            conn.commit()
            print(f"pins egress_ip remplis: {filled}")
            return 0
        if args.action == "export":
            if args.write:
                write_env_from_registry(Path(args.env), conn)
                count = len(env_station_lines(conn))
                print(f"pins exportés: {count} ligne(s) écrite(s) dans {args.env}")
                return 0
            for key, value in sorted(env_station_lines(conn).items()):
                print(f"{key}={value}")
            return 0
        for pin in _pins.list_pins(conn):
            print(
                f"station={pin.station} host={pin.hostname or '-'} "
                f"country={pin.country or '-'} ip={pin.egress_ip or '-'} "
                f"source={pin.source}"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
