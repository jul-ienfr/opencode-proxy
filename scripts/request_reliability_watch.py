#!/usr/bin/env python3
"""Veille de fiabilité free (feature 012, T027/T035).

Exécute périodiquement ``request_reliability_report.build_report`` sur une
fenêtre glissante et écrit le résultat JSON dans ``logs/reliability_24h.json``.
Après 24 h depuis la remise en route, le fichier contient une fenêtre 24 h
propre (sans les incidents d'avant le correctif).

Usage :
    python scripts/request_reliability_watch.py --once
    python scripts/request_reliability_watch.py --interval 3600 --hours 24
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from request_reliability_report import build_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--interval", type=float, default=3600.0)
    parser.add_argument("--clean-start", default="2026-11-09T08:20:00Z",
                        help="Début de la fenêtre propre (UTC, YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--out", default=str(ROOT / "logs" / "reliability_24h.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        report = build_report(max(0.1, float(args.hours)), clean_start=args.clean_start)
        report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.once:
            return 0 if report["verdict"] == "ok" else 1
        time.sleep(max(60.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
