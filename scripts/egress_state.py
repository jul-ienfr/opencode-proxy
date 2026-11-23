#!/usr/bin/env python3
"""État des buckets d'egress (023 T040).

Usage:
    python scripts/egress_state.py [--provider nordvpn] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:4000"


def _get(path: str) -> dict:
    with urllib.request.urlopen(API + path, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show egress bucket state")
    parser.add_argument("--provider", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = _get("/api/egress/state")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"proxy unavailable: {exc}", file=sys.stderr)
        return 2
    providers = data.get("providers") or {}
    if args.provider:
        providers = {args.provider: providers.get(args.provider, {})}
    if args.json:
        print(json.dumps(providers, indent=2, ensure_ascii=False))
        return 0
    for name, info in providers.items():
        buckets = info.get("buckets") or {}
        caps = info.get("capabilities") or {}
        print(
            f"{name:8} enabled={str(info.get('enabled')):5} "
            f"rot={str(caps.get('can_rotate')):5} quota={caps.get('quota_source')} "
            f"proven={buckets.get('proven', 0)} serving={buckets.get('serving', 0)} "
            f"spent={buckets.get('spent', 0)} burned={buckets.get('burned', 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
