"""Soak court des stations via /api/stations (011).

Usage :
    python scripts/station_soak.py --duration 300 --interval 15

Écrit un JSON de synthèse : disponibilité par provider, minimum observé,
doublons, buckets uniques, transitions.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "logs" / "station_soak.json"


def _fetch(api_base: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{api_base}/api/stations", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:4000")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--jsonl",
        default=str(ROOT / "logs" / "station_soak.jsonl"),
        help="journal append-only par échantillon",
    )
    args = parser.parse_args(argv)

    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    samples = 0
    per_provider: dict[str, list[int]] = {}
    min_usable: dict[str, int] = {}
    duplicate_samples = 0
    unique_buckets_min: dict[str, int] = {}
    states: Counter[str] = Counter()
    while time.time() - started < args.duration:
        try:
            payload = _fetch(args.api_base)
        except Exception as exc:
            states[f"error:{type(exc).__name__}"] += 1
            time.sleep(max(1.0, args.interval))
            continue
        samples += 1
        by_provider: dict[str, list[dict]] = {}
        for row in payload.get("stations") or []:
            by_provider.setdefault(str(row.get("provider")), []).append(row)
            states[str(row.get("state"))] += 1
        sample = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "providers": {},
            "duplicate_buckets": bool(payload.get("duplicate_buckets")),
        }
        for provider, rows in by_provider.items():
            usable = sum(1 for row in rows if row.get("client_usable"))
            per_provider.setdefault(provider, []).append(usable)
            min_usable[provider] = min(min_usable.get(provider, usable), usable)
            blocks = {str(row.get("block")) for row in rows if row.get("block")}
            unique_buckets_min[provider] = min(
                unique_buckets_min.get(provider, len(blocks)), len(blocks)
            )
            sample["providers"][provider] = {
                "usable": usable,
                "total": len(rows),
                "unique_buckets": len(blocks),
            }
        if payload.get("duplicate_buckets"):
            duplicate_samples += 1
        try:
            with jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        except OSError:
            pass
        time.sleep(max(1.0, args.interval))

    summary = {
        "ok": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "samples": samples,
        "per_provider": {
            provider: {
                "min_usable": min_usable.get(provider, 0),
                "avg_usable": round(sum(values) / len(values), 2) if values else 0,
                "max_usable": max(values) if values else 0,
                "unique_buckets_min": unique_buckets_min.get(provider, 0),
            }
            for provider, values in per_provider.items()
        },
        "duplicate_samples": duplicate_samples,
        "states": dict(states),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if duplicate_samples == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
