#!/usr/bin/env python3
"""Run one allocator canary against a single station.

The canary is deliberately explicit:

    python scripts/ip_registry_canary.py --dry-run
    python scripts/ip_registry_canary.py --station 1 \
        --hostname al110.nordvpn.com --egress 186.247.44.61

It seeds one known (hostname, egress) record, then runs the allocator
(claim → Compose bind → official probe → assign/release).  Use ``--dry-run``
first: it lists candidates without touching the station.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.ip_registry import (  # noqa: E402
    ComposeHostnameBinder,
    IpAllocator,
    IpRecord,
    IpState,
    NeedReason,
    SqliteIpRegistry,
    StationEgress,
    StationNeed,
    StationProbe,
)


def _load_cfg(path: Path) -> dict:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = data.get("ip_registry") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


async def _run(args) -> int:
    cfg = _load_cfg(Path(args.config))
    db_path = Path(args.db or cfg.get("db_path") or "logs/ip_registry.db")
    score = cfg.get("score") or {}
    eligibility = cfg.get("eligibility") or {}
    allocator_cfg = cfg.get("allocator") or {}
    registry = SqliteIpRegistry(
        str(db_path),
        error_cooldown_s=float(eligibility.get("error_cooldown_s", 1800) or 1800),
        min_score=float(score.get("min_score", 20) or 20),
    )
    try:
        now = time.time()
        existing = registry.get(args.egress) if args.egress else None
        if args.egress and args.hostname and existing is None:
            registry.upsert_record(
                IpRecord(
                    egress_ip=args.egress,
                    hostname=args.hostname,
                    country=args.country,
                    state=IpState.FRESH,
                ),
                now=now,
            )
        candidates = registry.eligible(now=now, limit=5)
        print(f"db={db_path} station={args.station} candidates={len(candidates)}")
        for candidate in candidates:
            print(
                f"  {candidate.egress_ip:15s} {candidate.hostname:28s} "
                f"{candidate.country:20s} state={candidate.state.value}"
            )
        if args.dry_run:
            return 0 if candidates else 1
        egress = StationEgress()
        binder = ComposeHostnameBinder(ROOT, egress=egress)
        probe = StationProbe(egress=egress)
        allocator = IpAllocator(
            registry,
            binder,
            probe,
            model=args.model,
            lease_ttl_s=float(cfg.get("lease_ttl_s", 300) or 300),
            max_attempts=int(
                args.max_attempts
                or allocator_cfg.get("max_attempts_per_need", 20)
                or 20
            ),
        )
        outcome = await allocator.allocate(
            StationNeed(args.station, NeedReason.MANUAL)
        )
        print(
            f"outcome ok={outcome.ok} station={outcome.station} "
            f"ip={outcome.egress_ip} host={outcome.hostname} "
            f"attempts={outcome.attempts} reason={outcome.reason}"
        )
        return 0 if outcome.ok else 1
    finally:
        registry.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=int, default=1)
    parser.add_argument("--hostname", default="al110.nordvpn.com")
    parser.add_argument("--egress", default="186.247.44.61")
    parser.add_argument("--country", default="Albania")
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--db", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
