"""Écrit un échantillon SLO dans ``registry.db`` (019 T302/T313).

À lancer chaque minute (planificateur, supervisor, cron). Lit l'état des
stations, le warm pool, les incidents et les rotations ; les compteurs
mémoire de l'app restent à 0 tant que ``/metrics`` n'est pas collecté.

T313 ajoute deux modes d'exploitation :
* ``--loop --interval 120`` : échantillonne toutes les 2 min et alerte dès
  que 6/6 n'est plus tenu sur ``--window`` (10 min par défaut) ;
* ``--check`` : un seul échantillon, exit 1 si 6/6 n'est pas tenu.

    python scripts/slo_sample.py
    python scripts/slo_sample.py --db logs/registry.db --window 600 --check
    python scripts/slo_sample.py --loop --interval 120 --window 600
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from free.qualification import slo_samples as _slo  # noqa: E402


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _sample_once(db_path: Path, window: float) -> tuple[object, bool]:
    conn = _connect(db_path)
    try:
        sample = _slo.sample_once(conn)
        conn.commit()
        held = _slo.six_of_six_held(conn, window_s=window)
        return sample, held
    finally:
        conn.close()


def _report(sample: object, held: bool, window: float) -> None:
    print(
        f"slo sampled_at={sample.sampled_at:.0f} "
        f"usable={sample.stations_client_usable}/6 "
        f"warm={sample.warm_pool_live} "
        f"200_rate={sample.ip_200_rate:.3f} "
        f"incidents={sample.incidents_open} "
        f"orphans={sample.rotation_jobs_orphans} "
        f"6/6_sur_{window:.0f}s={'oui' if held else 'NON'}",
        flush=True,
    )
    if not held:
        print(
            f"ALERTE SLO [{time.time():.0f}] 6/6 non tenu sur les "
            f"{window:.0f} dernières secondes — stations_client_usable="
            f"{sample.stations_client_usable}/6",
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "logs" / "registry.db"))
    parser.add_argument("--window", type=float, default=600.0)
    parser.add_argument(
        "--interval",
        type=float,
        default=120.0,
        help="période du mode --loop en secondes (défaut 120)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="échantillonne en continu jusqu'à Ctrl+C",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="retourne 1 si 6/6 n'est pas tenu sur la fenêtre",
    )
    args = parser.parse_args(argv)

    path = Path(args.db)
    if args.loop:
        interval = max(1.0, float(args.interval))
        try:
            while True:
                sample, held = _sample_once(path, args.window)
                _report(sample, held, args.window)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("stopped", flush=True)
            return 0

    sample, held = _sample_once(path, args.window)
    _report(sample, held, args.window)
    if args.check:
        return 0 if held else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
