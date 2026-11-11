#!/usr/bin/env python3
"""Canary release check for the six-station qualification pipeline.

Fetches qualification projections from the running dashboard and applies the
release contract in :func:`free.qualification.canary.evaluate_canary`.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from free.qualification.canary import evaluate_canary

ENDPOINTS = {
    "summary": "/api/egress/qualification/summary",
    "stations": "/api/egress/qualification/stations",
    "slo": "/api/egress/qualification/slo",
    "watchdog": "/api/egress/qualification/watchdog",
    "discovery": "/api/egress/qualification/discovery?limit=1",
    "transports": "/api/egress/qualification/transports",
    "rotations": "/api/egress/qualification/rotations?limit=50",
}


def fetch_json(
    base: str,
    path: str,
    *,
    token: str = "",
    timeout: float = 20.0,
) -> dict:
    request = urllib.request.Request(base.rstrip("/") + path)
    if token:
        request.add_header("X-Dashboard-Token", token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:4000")
    parser.add_argument("--token", default="")
    parser.add_argument("--target", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", default="logs/canary-014.json")
    args = parser.parse_args(argv)

    payloads: dict[str, dict] = {}
    errors: list[str] = []
    for name, path in ENDPOINTS.items():
        try:
            payloads[name] = fetch_json(
                args.api_base,
                path,
                token=args.token,
                timeout=args.timeout,
            )
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            payloads[name] = {}
            errors.append(f"{name}:{type(exc).__name__}:{exc}")

    report = evaluate_canary(
        payloads,
        target=args.target,
        fetch_errors=errors,
    )
    report["generated_at"] = datetime.now(UTC).isoformat()
    report["api_base"] = args.api_base

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
