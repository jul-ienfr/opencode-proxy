#!/usr/bin/env python3
"""Active/désactive un provider d'egress sans redémarrage (023 T017).

Usage:
    python scripts/egress_toggle.py --provider warp --disable
    python scripts/egress_toggle.py --provider direct --enable
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


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toggle an egress provider")
    parser.add_argument("--provider", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    args = parser.parse_args(argv)
    try:
        current = list(_get("/api/egress/config").get("egress_providers") or [])
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"proxy unavailable: {exc}", file=sys.stderr)
        return 2
    provider = args.provider.strip().lower()
    if args.enable and provider not in current:
        current.append(provider)
    if args.disable:
        current = [p for p in current if p != provider]
    if not current:
        print("refusing to disable every provider", file=sys.stderr)
        return 2
    try:
        result = _post("/api/egress/config", {"egress_providers": current})
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
