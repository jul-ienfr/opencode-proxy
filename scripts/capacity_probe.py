#!/usr/bin/env python3
"""Mesure la capacité réelle d'un bucket après les sondes — 023 T067.

Usage:
    python scripts/capacity_probe.py --station 1 --requests 10
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests as creq  # noqa: E402

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
CAPTURE = ROOT / "free" / "captures" / "2.3.0.json"
EVIDENCE = ROOT / "docs" / "evidence"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure per-bucket capacity")
    parser.add_argument("--station", type=int, required=True)
    parser.add_argument("--requests", type=int, default=10)
    args = parser.parse_args(argv)
    data = json.loads(CAPTURE.read_text(encoding="utf-8"))
    body = data["body_raw"]
    headers = {
        "authorization": "Bearer public",
        "content-type": "application/json",
        "user-agent": data.get("user_agent") or "opencode/latest/2.3.0/cli",
        "x-opencode-client": data.get("client") or "cli",
        "x-opencode-project": data.get("project") or "global",
    }
    socks = f"socks5h://127.0.0.1:{1079 + int(args.station)}"
    results = []
    for index in range(max(1, args.requests)):
        session = creq.Session(
            impersonate="chrome131",
            proxies={"http": socks, "https": socks},
            timeout=90,
        )
        try:
            resp = session.post(ENDPOINT, data=body.encode("utf-8"), headers=headers, stream=True)
            status = int(resp.status_code)
            results.append(
                {
                    "n": index + 1,
                    "status": status,
                    "retry_after": resp.headers.get("retry-after"),
                }
            )
            print(f"#{index + 1} HTTP {status} retry={resp.headers.get('retry-after')}")
            if status != 200:
                break
        finally:
            session.close()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE / f"capacity-station{args.station}-{stamp}.json"
    path.write_text(
        json.dumps({"station": args.station, "results": results}, indent=2),
        encoding="utf-8",
    )
    ok = sum(1 for r in results if r["status"] == 200)
    print(f"capacity_observed={ok} evidence={path}")
    # [fix 2026-11-21] Une station qui répond 429/erreur dès la première
    # requête mesurait `capacity_observed=0` mais sortait 0 : toute
    # automatisation (`capacity_probe.py && ...`) prenait une station brûlée
    # pour un succès. Aligner le code de sortie sur la mesure.
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
