#!/usr/bin/env python3
"""CLI for the NordVPN qualification queue.

Shadow mode (default) ingests the API snapshot, computes potential scores and
prints the queue summary/top candidates.  No tunnel is opened.

Live mode requires a station adapter; it is intentionally not wired to Docker
from this CLI.  Production wires ``free.qualification.runtime`` with the
existing Gluetun/manager adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.qualification import build_qualification, build_registry_station_adapter  # noqa: E402


def _load_config(path: str | Path) -> dict:
    try:
        import yaml
    except Exception:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--db", default="")
    parser.add_argument("--api-cache", default="")
    parser.add_argument("--station", type=int, default=0)
    parser.add_argument("--max", type=int, default=0, help="0 = all claimable")
    parser.add_argument("--epoch", default="")
    parser.add_argument("--ingest", action="store_true", default=True)
    parser.add_argument("--no-ingest", dest="ingest", action="store_false")
    parser.add_argument("--shadow", action="store_true", default=True)
    parser.add_argument("--live", dest="shadow", action="store_false")
    parser.add_argument("--discovery", action="store_true", help="run the discovery scheduler")
    parser.add_argument("--force", action="store_true", help="force discovery even if not due")
    parser.add_argument("--watchdog", action="store_true", help="probe stations and persist incidents")
    parser.add_argument("--watchdog-url", default="http://127.0.0.1:4000")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = _load_config(args.config)
    catalog = config.setdefault("egress_catalog", {})
    qualification = catalog.setdefault("qualification", {})
    qualification.setdefault("enabled", True)
    qualification.setdefault("shadow_mode", True)
    if args.api_cache:
        qualification["api_cache"] = args.api_cache
    if args.station:
        qualification["station"] = args.station
    if args.max:
        qualification["max_hosts"] = args.max

    runtime = build_qualification(config, db_path=args.db or None)
    if runtime is None:
        print("qualification disabled", file=sys.stderr)
        return 2
    if not args.shadow:
        runtime.sweep.adapter = build_registry_station_adapter(
            config,
            station=args.station or 1,
        )
    try:
        discovery = None
        if args.discovery:
            discovery = runtime.discovery.run(force=args.force)
            ingested = int(discovery.server_count)
        else:
            ingested = runtime.ingest() if args.ingest else 0
        summary = runtime.summary()
        top: list[dict[str, Any]] = [
            {
                "hostname": candidate.hostname,
                "country": candidate.country,
                "load": candidate.load,
                "transports": [str(item) for item in candidate.transports],
                "potential_score": candidate.potential_score,
                "status": str(candidate.status),
            }
            for candidate in runtime.store.top_candidates(args.top)
        ]
        result = None
        watchdog = None
        if args.watchdog:
            from free.qualification import (
                HttpStationProbe,
                WatchdogConfig,
                WatchdogService,
                build_supervisor_repair_adapter,
            )

            runtime.watchdog = WatchdogService(
                runtime.store,
                HttpStationProbe(base_url=args.watchdog_url),
                build_supervisor_repair_adapter(),
                config=WatchdogConfig(
                    stations=runtime.watchdog.config.stations,
                    max_repair_attempts=runtime.watchdog.config.max_repair_attempts,
                ),
            )
            watchdog = asyncio.run(runtime.watchdog.run_once())
        elif not args.shadow:
            if runtime.sweep.adapter is None:
                print("live mode requires a station adapter", file=sys.stderr)
                return 2
            result = asyncio.run(
                runtime.sweep.run_epoch(
                    epoch_id=args.epoch,
                    max_hosts=args.max or None,
                    shadow=False,
                )
            )
        payload = {
            "ingested": ingested,
            "summary": {
                **summary,
                "by_status": {str(key): value for key, value in summary["by_status"].items()},
            },
            "top": top,
            "discovery": discovery.snapshot() if discovery is not None else None,
            "watchdog": watchdog.snapshot() if watchdog is not None else None,
            "watchdog_state": runtime.store.watchdog_state() if watchdog is not None else None,
            "stations": runtime.store.list_station_states() if watchdog is not None else [],
            "sweep": result.snapshot() if result is not None else None,
        }
        if args.json:
            _print(payload)
        else:
            print(
                f"ingested={ingested} total={summary['total']} pending={summary['pending']} "
                f"usable={summary['usable']} quota={summary['quota']} tested={summary['tested']}"
            )
            for item in top[:5]:
                print(
                    f"  {item['potential_score']:6.2f}  {item['hostname']:<24} "
                    f"{item['country']:<18} load={item['load']:>5.0f} "
                    f"{','.join(str(transport) for transport in item['transports'])}"
                )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
