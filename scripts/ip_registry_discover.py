#!/usr/bin/env python3
"""Targeted discovery: bind catalog servers, verify egress, probe free.

This is the bridge between the catalog and ``ip_state``: it draws a small
number of NordVPN hostnames (low load first), recreates one station for each,
records the observed egress, probes the official free body, and stores the
result in the registry.

Usage:
    python scripts/ip_registry_discover.py --dry-run
    python scripts/ip_registry_discover.py --station 1 --country Albania --limit 5
    python scripts/ip_registry_discover.py --station 1 --limit 5 --restore
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.ip_registry import (  # noqa: E402
    ComposeHostnameBinder,
    IpRecord,
    IpState,
    SqliteIpRegistry,
    StationEgress,
    StationProbe,
)


def _cfg() -> dict:
    try:
        import yaml

        data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        section = data.get("ip_registry") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


async def _run(args) -> int:
    from free.ip_registry.catalog import NordVpnCatalog

    cfg = _cfg()
    catalog = NordVpnCatalog(ttl_s=float(cfg.get("catalog_refresh_s", 21600) or 21600))
    servers = await catalog.refresh()
    if not servers:
        print("catalog empty")
        return 1
    selected = [s for s in servers if not args.country or s.country == args.country]
    selected.sort(key=lambda s: (s.load, s.hostname))
    selected = selected[: max(1, args.limit)]
    print(f"catalog={len(servers)} selected={len(selected)} country={args.country or '*'}")
    for server in selected:
        print(f"  {server.hostname:28s} {server.country:20s} load={server.load:g}")
    if args.dry_run:
        return 0

    db_path = Path(args.db or cfg.get("db_path") or "logs/ip_registry.db")
    score = cfg.get("score") or {}
    eligibility = cfg.get("eligibility") or {}
    registry = SqliteIpRegistry(
        str(db_path),
        error_cooldown_s=float(eligibility.get("error_cooldown_s", 1800) or 1800),
        min_score=float(score.get("min_score", 20) or 20),
    )
    egress = StationEgress()
    binder = ComposeHostnameBinder(ROOT, egress=egress)
    probe = StationProbe(egress=egress)
    try:
        for server in selected:
            now = time.time()
            print(f"\n=== {server.hostname} ===", flush=True)
            bind = await binder.assign(args.station, server.hostname)
            if not bind.ok or not bind.egress_ip:
                print(f"  bind rejected: {bind.error or 'no-egress'}")
                continue
            ip = bind.egress_ip
            print(f"  egress={ip}")
            record = registry.get(ip)
            if record is None:
                registry.upsert_record(
                    IpRecord(
                        egress_ip=ip,
                        hostname=server.hostname,
                        country=server.country,
                        state=IpState.FRESH,
                    ),
                    now=now,
                )
            lease = registry.claim(ip, args.station, 300.0, now)
            if lease is None:
                current = registry.get(ip)
                print(f"  claim refused (state={current.state.value if current else '?'})")
                continue
            result = await probe.probe(args.station, args.model)
            registry.record(lease, result)
            registry.release(lease, f"discover_{result.status}", now)
            current = registry.get(ip)
            print(
                f"  free={result.status} ra={result.retry_after_s:g} "
                f"state={current.state.value if current else '?'} "
                f"reset={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(current.reset_at)) if current and current.reset_at else '-'}"
            )
    finally:
        registry.close()
        if args.restore:
            service = binder.service_name(args.station)
            print(f"\nrestoring {service} with base compose…")
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "up",
                    "-d",
                    "--force-recreate",
                    "--no-deps",
                    service,
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=240,
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=int, default=1)
    parser.add_argument("--country", default="Albania")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--db", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
