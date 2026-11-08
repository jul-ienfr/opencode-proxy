"""Audit CLI des stations NordVPN et WARP (feature 011).

Lit la même vérité que ``/api/stations`` et imprime un rapport JSON ou texte.
Aucune action Docker n'est exécutée ; ``--fix`` est réservé à une version
ultérieure et refuse explicitement pour l'instant.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_API = "http://127.0.0.1:4000"


def fetch_stations(api_base: str = DEFAULT_API) -> dict[str, Any]:
    with urllib.request.urlopen(f"{api_base}/api/stations", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def build_report(payload: dict[str, Any], provider: str | None = None) -> dict[str, Any]:
    stations = list(payload.get("stations") or [])
    if provider:
        stations = [item for item in stations if item.get("provider") == provider]
    usable = [item for item in stations if item.get("client_usable")]
    total = len(stations)
    unique_buckets = {
        str(item.get("block"))
        for item in stations
        if item.get("block")
    }
    duplicates = payload.get("duplicate_buckets") or {}
    target = int(payload.get("target_slots", 6) or 6)
    if total and len(usable) >= target and not duplicates:
        verdict = "ok"
    elif len(usable) >= target:
        verdict = "degraded"
    else:
        verdict = "insufficient"
    return {
        "ok": verdict == "ok",
        "generated_at": payload.get("generated_at"),
        "target_slots": target,
        "provider": provider,
        "configured": total,
        "client_usable": len(usable),
        "unique_buckets": len(unique_buckets),
        "duplicates": duplicates,
        "verdict": verdict,
        "stations": stations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit des stations NordVPN/WARP")
    parser.add_argument("--api-base", default=DEFAULT_API)
    parser.add_argument("--provider", choices=["nordvpn", "warp"], default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on", choices=["degraded", "insufficient"], default=None)
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.fix:
        print("--fix n'est pas encore implémenté (011 Phase 2).", file=sys.stderr)
        return 2
    try:
        payload = fetch_stations(args.api_base)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"API indisponible: {exc}", file=sys.stderr)
        return 2
    report = build_report(payload, args.provider)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"{report['verdict']}: {report['client_usable']}/{report['target_slots']} "
            f"client-utilisables, {report['unique_buckets']} bucket(s) unique(s)"
        )
        for station in report["stations"]:
            print(
                f"- {station.get('provider')}#{station.get('station')} "
                f"{station.get('state')} usable={station.get('client_usable')} "
                f"ip={station.get('egress_ip')} block={station.get('block')}"
            )
    if args.fail_on == "degraded" and report["verdict"] in ("degraded", "insufficient"):
        return 1
    if args.fail_on == "insufficient" and report["verdict"] == "insufficient":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
