#!/usr/bin/env python3
"""Phase 1 observe pass: import the NordVPN catalog into the IP registry.

No runtime impact: this only fills ``servers`` and reports counts.  It does
not claim, assign or touch any station.

Usage:
    python scripts/ip_registry_observe.py
    python scripts/ip_registry_observe.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.ip_registry.catalog import NordVpnCatalog  # noqa: E402
from free.ip_registry.config import load_registry_config  # noqa: E402
from free.ip_registry.sqlite_registry import SqliteIpRegistry  # noqa: E402


async def _run(args) -> int:
    cfg = load_registry_config(args.config or None)
    db_path = Path(args.db or cfg.get("db_path") or "logs/ip_registry.db")
    catalog = NordVpnCatalog.from_config(cfg)
    servers = await catalog.refresh()
    print(f"catalog: {len(servers)} online servers")
    print(
        f"source: {catalog.source_id or '-'} stale={catalog.stale} "
        f"etag={catalog.etag or '-'}"
    )
    if catalog.last_error:
        print(f"catalog error: {catalog.last_error}")
    if args.dry_run:
        for server in servers[:10]:
            print(f"  {server.hostname} {server.country} load={server.load:g}")
        return 0 if servers else 1

    registry = SqliteIpRegistry.from_config(cfg, db_path=db_path)
    try:
        resolution = catalog.last_resolution
        if resolution is not None:
            count = registry.sync_snapshot(resolution, authoritative=True)
        else:
            count = registry.sync_catalog(
                servers,
                source_id=catalog.source_id or "nordvpn_api_servers",
                stale=catalog.stale,
                catalog_fetched_at=catalog.catalog_fetched_at,
            )
        pruned = registry.prune_history(
            float(cfg.get("retention_days", 7) or 7)
        )
        rows = registry._conn.execute(
            "SELECT state, COUNT(*) AS n FROM ip_state GROUP BY state ORDER BY state"
        ).fetchall()
        print(f"synced: {count} servers into {db_path}")
        print("sources:", registry.catalog_source_counts() or "empty")
        print("ip_state:", {row["state"]: row["n"] for row in rows} or "empty")
        print(f"eligible: {len(registry.eligible(now=None, limit=1000))}")
        if pruned["observations"] or pruned["assignments"]:
            print(f"pruned: {pruned}")
    finally:
        registry.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--db", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
