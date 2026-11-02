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
    Server,
    SqliteIpRegistry,
    StationEgress,
    StationProbe,
)
from free.ip_registry.config import load_registry_config  # noqa: E402


async def _run(args) -> int:
    from free.ip_registry.catalog import NordVpnCatalog

    cfg = load_registry_config()
    catalog = NordVpnCatalog.from_config(cfg)
    servers = await catalog.refresh()
    if not servers:
        print("catalog empty")
        return 1
    if args.hostnames:
        selected = [
            Server(hostname=name.strip(), country=args.country or "Albania")
            for name in args.hostnames.split(",")
            if name.strip()
        ]
    else:
        selected = [s for s in servers if not args.country or s.country == args.country]
        selected.sort(key=lambda s: (s.load, s.hostname))
        selected = selected[: max(1, args.limit)]
    print(f"catalog={len(servers)} selected={len(selected)} country={args.country or '*'}")
    for server in selected:
        print(f"  {server.hostname:28s} {server.country:20s} load={server.load:g}")
    if args.dry_run:
        return 0

    db_path = Path(args.db or cfg.get("db_path") or "logs/ip_registry.db")
    registry = SqliteIpRegistry.from_config(cfg, db_path=db_path)
    egress = StationEgress()
    binder = ComposeHostnameBinder(ROOT, egress=egress)
    probe = StationProbe(
        egress=egress,
        timeout_s=float(cfg.get("test_timeout_s", 60) or 60),
    )
    try:
        resolution = catalog.last_resolution
        if resolution is not None:
            synced = registry.sync_snapshot(resolution, authoritative=True)
            print(
                f"catalog synced: {synced} servers "
                f"source={resolution.source_id} stale={resolution.stale}"
            )
        pruned = registry.prune_history(
            float(cfg.get("retention_days", 7) or 7)
        )
        if pruned["observations"] or pruned["assignments"]:
            print(f"pruned: {pruned}")
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
    parser.add_argument("--hostnames", default="")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--db", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
