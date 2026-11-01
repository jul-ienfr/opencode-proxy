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
from free.ip_registry.sqlite_registry import SqliteIpRegistry  # noqa: E402


def _load_cfg(path: Path) -> dict:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = data.get("ip_registry") or {}
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        print(f"config load failed: {type(exc).__name__}: {exc}")
        return {}


async def _run(args) -> int:
    cfg = _load_cfg(Path(args.config))
    db_path = Path(args.db or cfg.get("db_path") or "logs/ip_registry.db")
    catalog = NordVpnCatalog(ttl_s=float(cfg.get("catalog_refresh_s", 21600) or 21600))
    servers = await catalog.refresh()
    print(f"catalog: {len(servers)} online servers")
    if catalog.last_error:
        print(f"catalog error: {catalog.last_error}")
    if args.dry_run:
        for server in servers[:10]:
            print(f"  {server.hostname} {server.country} load={server.load:g}")
        return 0 if servers else 1

    score = cfg.get("score") or {}
    eligibility = cfg.get("eligibility") or {}
    registry = SqliteIpRegistry(
        str(db_path),
        error_cooldown_s=float(eligibility.get("error_cooldown_s", 1800) or 1800),
        min_score=float(score.get("min_score", 20) or 20),
    )
    try:
        count = registry.sync_catalog(servers)
        rows = registry._conn.execute(
            "SELECT state, COUNT(*) AS n FROM ip_state GROUP BY state ORDER BY state"
        ).fetchall()
        print(f"synced: {count} servers into {db_path}")
        print("ip_state:", {row["state"]: row["n"] for row in rows} or "empty")
        print(f"eligible: {len(registry.eligible(now=None, limit=1000))}")
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
