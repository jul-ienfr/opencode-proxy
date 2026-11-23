#!/usr/bin/env python3
"""Soak « zéro 429 client » — 023 T072 / SC-001.

Toutes les 60 s : lit la réserve prouvée et les 429 classés. Échoue si un 429
client est observé alors que la réserve était >= 1.

Usage:
    python scripts/soak_zero_429.py --hours 24 --require-reserve 1
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:4000"
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "logs" / "requests.db"


def _get(path: str) -> dict:
    with urllib.request.urlopen(API + path, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _reserve() -> int:
    data = _get("/api/egress/state")
    total = 0
    for info in (data.get("providers") or {}).values():
        buckets = info.get("buckets") or {}
        total += int(buckets.get("proven", 0)) + int(buckets.get("serving", 0))
    return total


def _client_429_since(ts: str) -> int | None:
    if not DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = con.execute(
            "SELECT COUNT(*) FROM requests WHERE final_status='429' AND timestamp >= ?",
            (ts,),
        ).fetchone()
        con.close()
        return int(row[0]) if row else 0
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Soak zero client 429")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--require-reserve", type=int, default=1)
    args = parser.parse_args(argv)
    started = dt.datetime.now(dt.UTC)
    started_iso = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = time.time() + max(0.1, args.hours) * 3600
    violations = 0
    reads_ok = 0
    proxy_errors = 0
    while time.time() < deadline:
        try:
            reserve = _reserve()
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"proxy unavailable: {exc}")
            proxy_errors += 1
            reserve = 0
        observed = _client_429_since(started_iso)
        if observed is not None:
            reads_ok += 1
        line = (
            f"{dt.datetime.now(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"reserve={reserve} client_429={observed}"
        )
        print(line, flush=True)
        if observed and reserve >= args.require_reserve:
            violations += 1
        time.sleep(60)
    # Un soak qui n'a jamais lu la base ne prouve rien : pas de PASS « à vide ».
    # [fix 2026-11-21] `proxy_errors` entre dans le verdict : quand le proxy est
    # injoignable, `reserve` est forcé à 0, ce qui désactive aussi le test de
    # violation (`reserve >= require_reserve`). Une fenêtre partiellement
    # aveugle ne peut pas certifier « zéro 429 client ».
    if violations:
        code = 1
    elif proxy_errors:
        code = 3
    elif reads_ok == 0:
        code = 2
    else:
        code = 0
    print(
        json.dumps(
            {
                "violations": violations,
                "reads_ok": reads_ok,
                "proxy_errors": proxy_errors,
                "ok": code == 0,
                "started": started_iso,
            }
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
