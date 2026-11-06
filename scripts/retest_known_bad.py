#!/usr/bin/env python3
"""Retest ciblé d'hostnames datacenter déjà classés 429.

Pour chaque hostname : rebind d'une station, lecture de l'egress réel, puis
UNE requête avec la capture mintée 2.2.5 (session neuve par requête). Le but
est de distinguer un 429 de format (session) d'un 429 de quota IP.

Exemple :
    python scripts/retest_known_bad.py \
        --hosts us14166.nordvpn.com,au1103.nordvpn.com,bb4.nordvpn.com,se-uk4.nordvpn.com,bw2.nordvpn.com \
        --stations 2,3,4,5,6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from test_free_egress import CAPTURE, load_capture, post_once  # noqa: E402
from test_selection_cycle import recreate, service_for, wait_egress  # noqa: E402

CATALOG = ROOT / "logs" / "nordvpn_classified.json"
OUT = ROOT / "logs" / "datacenter_retest.json"


def country_for(hostname: str) -> str:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    for server in data.get("servers") or []:
        if str(server.get("hostname") or "").lower() == hostname.lower():
            return str(server.get("country") or "")
    raise SystemExit(f"hostname inconnu dans le catalogue: {hostname}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", required=True, help="liste séparée par des virgules")
    parser.add_argument("--stations", default="2,3,4,5,6", help="stations correspondantes")
    parser.add_argument(
        "--countries",
        help="liste séparée par des virgules, prioritaire sur le catalogue",
    )
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--egress-timeout", type=float, default=120.0)
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    stations = [int(s.strip()) for s in args.stations.split(",") if s.strip()]
    if len(hosts) != len(stations):
        raise SystemExit("--hosts et --stations doivent avoir la même longueur")
    countries = [c.strip() for c in (args.countries or "").split(",") if c.strip()]
    if len(countries) == 1:
        countries = countries * len(hosts)
    if countries and len(countries) != len(hosts):
        raise SystemExit("--countries doit avoir 1 valeur ou la même longueur que --hosts")

    capture = load_capture(CAPTURE)
    if capture is None:
        raise SystemExit(f"capture absente: {CAPTURE}")

    results = []
    for index, (host, station) in enumerate(zip(hosts, stations)):
        country = countries[index] if countries else country_for(host)
        print(f"bind station {station} -> {host} ({country})", flush=True)
        recreate(station, host, country)
        egress = wait_egress(station, args.egress_timeout)
        if not egress:
            result = {
                "station": station,
                "host": host,
                "country": country,
                "egress": "",
                "status": "no-egress",
            }
            print("  no-egress", flush=True)
        else:
            proxy = f"socks5h://127.0.0.1:{1079 + station}"
            result = post_once(
                proxy,
                args.model,
                args.timeout,
                capture=capture,
                reuse_session=False,
            )
            result.update(
                {
                    "station": station,
                    "host": host,
                    "country": country,
                    "egress": egress,
                }
            )
            print("  " + json.dumps(result, ensure_ascii=False), flush=True)
        results.append(result)
        if result.get("status") == 200:
            print(f"  WINNER station {station} {host} egress {result.get('egress')}", flush=True)

    Path(args.output).write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"saved {args.output}", flush=True)
    return 0 if any(r.get("status") == 200 for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
