#!/usr/bin/env python3
"""Sonde la liveness d'un snapshot de proxys publics via l'egress réel.

Lit ``logs/proxy_lists/pool.jsonl`` (produit par ``fetch_proxy_lists.py``),
résout l'IP de sortie de chaque proxy sur ``https://api64.ipify.org`` et écrit :

- ``probe.json``   : résumé + détail des vivants
- ``alive.jsonl``  : endpoints vivants avec egress, famille et latence
- ``alive.txt``    : URLs prêtes pour ``scripts/test_proxy_pool.py``

Cette étape ne consomme aucun quota free : elle ne fait qu'un GET statique.
La validation métier (free 200) reste ``scripts/test_proxy_pool.py``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "logs" / "proxy_lists" / "pool.jsonl"
DEFAULT_OUT = ROOT / "logs" / "proxy_lists"
EGRESS_URL = "https://api64.ipify.org"


def _family(value: str) -> str | None:
    try:
        return "ipv6" if ipaddress.ip_address(value).version == 6 else "ipv4"
    except ValueError:
        return None


def _load(path: Path, per_scheme: int, seed: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("url") and row.get("scheme"):
            rows.append(row)
    if per_scheme <= 0:
        return rows
    rng = random.Random(seed)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["scheme"]), []).append(row)
    sample: list[dict[str, Any]] = []
    for scheme in sorted(grouped):
        bucket = grouped[scheme]
        rng.shuffle(bucket)
        sample.extend(bucket[:per_scheme])
    return sample


def _probe(row: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.time()
    url = str(row["url"])
    result: dict[str, Any] = {
        "url": url,
        "scheme": row.get("scheme"),
        "host": row.get("host"),
        "port": row.get("port"),
        "sources": row.get("sources") or [],
    }
    try:
        from curl_cffi import requests
        from curl_cffi.const import CurlOpt

        response = requests.get(
            EGRESS_URL,
            proxies={"http": url, "https": url},
            timeout=timeout,
            impersonate="chrome131",
            curl_options={
                CurlOpt.PROXY_SSL_VERIFYPEER: 0,
                CurlOpt.PROXY_SSL_VERIFYHOST: 0,
            },
        )
        text = str(getattr(response, "text", "") or "").strip()
        family = _family(text)
        if response.status_code == 200 and family:
            result.update({"status": "alive", "egress": text, "family": family})
        else:
            result.update({"status": "dead", "error": f"http {response.status_code}"})
    except Exception as exc:
        result.update({"status": "dead", "error": f"{type(exc).__name__}: {exc}"[:200]})
    result["ms"] = round((time.time() - started) * 1000)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--per-scheme", type=int, default=200, help="0 = tous")
    parser.add_argument("--concurrency", type=int, default=80)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    source = Path(args.input)
    if not source.exists():
        print(f"snapshot absent: {source}", file=sys.stderr)
        return 2
    rows = _load(source, args.per_scheme, args.seed)
    if not rows:
        print("aucun endpoint à sonder", file=sys.stderr)
        return 2

    started = time.time()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for result in pool.map(lambda row: _probe(row, args.timeout), rows):
            results.append(result)
            if result["status"] != "alive":
                print(
                    f"dead  {result.get('scheme'):7} {result.get('host')}:{result.get('port')} "
                    f"{result.get('error', '')}",
                    file=sys.stderr,
                    flush=True,
                )

    alive = [row for row in results if row["status"] == "alive"]
    seen: dict[str, dict[str, Any]] = {}
    for row in sorted(alive, key=lambda item: item.get("ms", 10**9)):
        seen.setdefault(str(row["egress"]), row)

    per_scheme: dict[str, dict[str, int]] = {}
    for row in results:
        bucket = per_scheme.setdefault(str(row["scheme"]), {"tested": 0, "alive": 0, "dead": 0})
        bucket["tested"] += 1
        bucket["alive" if row["status"] == "alive" else "dead"] += 1
    for scheme, bucket in per_scheme.items():
        bucket["unique_egress"] = len({str(row["egress"]) for row in alive if row["scheme"] == scheme})

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    alive_jsonl = out_dir / "alive.jsonl"
    alive_jsonl.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in alive),
        encoding="utf-8",
    )
    alive_txt = out_dir / "alive.txt"
    alive_txt.write_text("".join(str(row["url"]) + "\n" for row in alive), encoding="utf-8")
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.time() - started, 2),
        "input": str(source),
        "tested": len(results),
        "alive": len(alive),
        "dead": len(results) - len(alive),
        "alive_pct": round(100.0 * len(alive) / max(1, len(results)), 2),
        "unique_egress": len(seen),
        "per_scheme": dict(sorted(per_scheme.items())),
        "alive_endpoints": alive,
    }
    report_path = out_dir / "probe.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "tested": report["tested"],
                "alive": report["alive"],
                "alive_pct": report["alive_pct"],
                "unique_egress": report["unique_egress"],
                "per_scheme": report["per_scheme"],
            },
            ensure_ascii=False,
        )
    )
    print(f"saved {report_path}")
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
