#!/usr/bin/env python3
"""Attend l'échéance SLO 24 h, puis écrit le rapport final dans docs/.

Le watcher (`station_slo_watch.py`) continue de collecter ; ce processus ne
fait que temporiser puis produire le Markdown. Il peut être relancé après un
redémarrage machine : si l'échéance est déjà passée, le rapport est écrit
immédiatement.

Usage :
    python scripts/station_slo_finalize.py \
        --since 2026-10-26T12:45:00Z --until 2026-10-27T12:45:00Z
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

from station_slo_report import ROOT, build_report


def _parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--in", dest="inp", default="logs/station_slo.jsonl")
    ap.add_argument(
        "--out",
        default="docs/POST_MORTEM_SLO_FINAL_2026-10-27.md",
    )
    args = ap.parse_args()
    until = _parse_iso(args.until)
    while True:
        now = dt.datetime.now(dt.UTC)
        remaining = (until - now).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(3600.0, max(5.0, remaining)))

    report = build_report(args.inp, args.since, args.until)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        "# Rapport SLO final — fenêtre 24 h\n\n"
        f"- Généré : `{generated}`\n"
        f"- Fenêtre demandée : `{args.since}` → `{args.until}`\n"
        f"- Source : `{args.inp}`\n\n"
    )
    out.write_text(header + report + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
