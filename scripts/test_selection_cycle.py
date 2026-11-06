#!/usr/bin/env python3
"""Cycle de sélection (one-off) : teste des hostnames NordVPN diversifiés.

Respecte le plan : types pondérés, pays distincts, exclusion des pools
`187.14.235.0/24` et `187.13.20.0/24`, ledger `/32` consulté avant probe.

Ce n'est pas le sélecteur de production : c'est un harnais de test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.ip_registry.adapters.probe import StationProbe


def known_bad_ips() -> dict[str, float]:
    conn = sqlite3.connect(ROOT / "logs" / "ip_registry.db")
    rows = conn.execute(
        "select egress_ip, max(reset_at) from observations "
        "where status=429 group by egress_ip"
    ).fetchall()
    conn.close()
    now = time.time()
    return {
        str(ip): float(reset or 0)
        for ip, reset in rows
        if reset and float(reset) > now
    }


def observed_hosts() -> set[str]:
    conn = sqlite3.connect(ROOT / "logs" / "ip_registry.db")
    rows = conn.execute("select distinct lower(hostname) from observations").fetchall()
    conn.close()
    return {str(row[0]) for row in rows if row[0]}


def pick_hosts(limit: int) -> list[dict]:
    catalog = json.loads((ROOT / "logs" / "nordvpn_classified.json").read_text(encoding="utf-8"))
    seen = observed_hosts()
    bad_prefixes = ("187.14.235.", "187.13.20.")
    excluded_countries = {"United States", "Australia"}
    pools = [
        (["standard", "p2p"], max(1, int(limit * 0.60))),
        (["dedicated"], max(1, int(limit * 0.20))),
        (["double"], max(1, int(limit * 0.20))),
    ]
    picked: list[dict] = []
    used_countries: set[str] = set()
    used_24: set[str] = set()
    for types, quota in pools:
        candidates = [
            s
            for s in catalog.get("servers") or []
            if s.get("status") == "online"
            and set(s.get("types") or []) & set(types)
            and s.get("country") not in excluded_countries
            and str(s.get("hostname") or "").lower() not in seen
            and not str(s.get("station") or "").startswith(bad_prefixes)
            and s.get("country") not in used_countries
            and ".".join(str(s.get("station") or "").split(".")[:3]) not in used_24
        ]
        candidates.sort(key=lambda s: (float(s.get("load") or 100), s.get("hostname") or ""))
        for server in candidates[:quota]:
            picked.append(server)
            used_countries.add(server.get("country") or "")
            used_24.add(".".join(str(server.get("station") or "").split(".")[:3]))
    random.shuffle(picked)
    return picked[:limit]


def service_for(station: int) -> str:
    return "vpn-gluetun" if station == 1 else f"vpn-gluetun-{station}"


def recreate(station: int, host: str, country: str) -> None:
    env = os.environ.copy()
    env[f"SERVER_HOSTNAMES_STATION{station}"] = host
    env[f"SERVER_COUNTRIES_STATION{station}"] = country
    subprocess.run(
        [
            "docker",
            "compose",
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            service_for(station),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def wait_egress(station: int, timeout_s: float = 60.0) -> str:
    deadline = time.time() + timeout_s
    port = 1079 + station
    while time.time() < deadline:
        try:
            result = subprocess.run(
                [
                    "curl.exe",
                    "-s",
                    "-m",
                    "5",
                    "--socks5-hostname",
                    f"127.0.0.1:{port}",
                    "https://api.ipify.org",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ip = (result.stdout or "").strip()
            if ip and ip.count(".") == 3:
                return ip
        except Exception:
            pass
        time.sleep(3)
    return ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--station", type=int, default=4)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    args = parser.parse_args()

    hosts = pick_hosts(args.limit)
    bad = known_bad_ips()
    results = []
    print("cycle", len(hosts), "hosts on station", args.station, flush=True)
    for server in hosts:
        host = str(server.get("hostname") or "")
        country = str(server.get("country") or "")
        types = ",".join(server.get("types") or [])
        print("bind", host, country, types, flush=True)
        recreate(args.station, host, country)
        egress = wait_egress(args.station)
        if not egress:
            print("  no-egress", host, flush=True)
            results.append({"host": host, "country": country, "types": types, "status": "no-egress"})
            continue
        if egress in bad:
            print("  skip known 429", egress, flush=True)
            results.append(
                {"host": host, "country": country, "types": types, "egress": egress, "status": "skip-429"}
            )
            continue
        probe = StationProbe(timeout_s=35.0, validate_sse=False)
        result = await probe.probe(args.station, args.model)
        entry = {
            "host": host,
            "country": country,
            "types": types,
            "egress": egress,
            "status": result.status,
            "retry_after": result.retry_after_s,
            "latency_ms": result.latency_ms,
        }
        results.append(entry)
        print("  egress", egress, "status", result.status, "ra", result.retry_after_s, flush=True)
        if result.status == 200:
            print("WINNER", json.dumps(entry), flush=True)
            break
    (ROOT / "logs" / "selection_cycle.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
