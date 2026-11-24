#!/usr/bin/env python3
"""Moniteur SLO de la flotte free — feature 019, tâches T302/T313.

Écrit un échantillon (par défaut toutes les 2 min) dans
``registry.db/slo_samples`` et alerte lorsque six stations ne sont pas
utilisables sur le même cycle pendant 10 minutes consécutives. Lecture seule
du registre ; la seule écriture est la table d'échantillons.

Exemples :
    python scripts/slo_monitor.py --once
    python scripts/slo_monitor.py --loop 120
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from observability.fleet_slo import collect, render_prometheus, write_sample  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "logs", "registry.db")
TARGET_USABLE = 6
ALERT_WINDOW_S = 600.0
SAMPLE_INTERVAL_S = 120.0


def _recent_samples(
    db_path: str, *, now: float, window_s: float = ALERT_WINDOW_S
) -> list[tuple[float, int]]:
    cutoff = now - (window_s + SAMPLE_INTERVAL_S)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            return [
                (float(ts), int(value))
                for ts, value in conn.execute(
                    "SELECT sampled_at, station_client_usable FROM slo_samples "
                    "WHERE sampled_at>=? ORDER BY sampled_at",
                    (cutoff,),
                )
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _alert_if_sustained(
    db_path: str,
    *,
    now: float | None = None,
    window_s: float = ALERT_WINDOW_S,
) -> str | None:
    """Alerte si ``< 6/6`` dure depuis ``window_s`` sans interruption.

    La durée est mesurée sur les horodatages, pas sur un nombre d'échantillons :
    le job peut tourner toutes les 1 ou 2 minutes sans changer le seuil.
    """

    ts = time.time() if now is None else float(now)
    samples = _recent_samples(db_path, now=ts, window_s=window_s)
    if len(samples) < 3:
        return None
    if any(value >= TARGET_USABLE for _, value in samples):
        return None
    oldest = samples[0][0]
    if ts - oldest < window_s:
        return None
    return (
        f"ALERT fleet_slo: {TARGET_USABLE}/6 not reached for "
        f"{int(ts - oldest)}s ({len(samples)} samples, last={samples[-1][1]})"
    )


def run_once(db_path: str, *, prometheus: bool = False) -> dict[str, float]:
    metrics = collect(db_path)
    write_sample(db_path, metrics)
    if prometheus:
        sys.stdout.write(render_prometheus(metrics))
    else:
        summary = " ".join(f"{k}={v:g}" for k, v in metrics.items())
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {summary}")
    alert = _alert_if_sustained(db_path)
    if alert:
        print(alert, file=sys.stderr)
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--once", action="store_true", help="un échantillon puis sortir")
    parser.add_argument("--loop", type=float, default=0.0, metavar="SECONDS")
    parser.add_argument("--prometheus", action="store_true")
    args = parser.parse_args(argv)
    if args.loop and args.loop > 0:
        while True:
            run_once(args.db, prometheus=args.prometheus)
            time.sleep(max(5.0, args.loop))
    run_once(args.db, prometheus=args.prometheus)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
