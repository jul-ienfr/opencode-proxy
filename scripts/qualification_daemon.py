#!/usr/bin/env python3
"""Periodic qualification daemon.

Shadow mode is offline-safe: it ingests the API cache and cycles the queue
without opening a tunnel.  Live mode uses the existing registry station
adapter.  The daemon never runs in the request path.
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

from free.qualification import (  # noqa: E402
    HttpStationProbe,
    build_qualification,
    build_qualification_scheduler,
    build_registry_station_adapter,
)


def _load_config(path: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except Exception:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _configure(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    catalog = config.setdefault("egress_catalog", {})
    qualification = catalog.setdefault("qualification", {})
    qualification["enabled"] = True
    qualification["shadow_mode"] = bool(args.shadow)
    qualification["station"] = int(args.station)
    qualification["max_hosts"] = int(args.max)
    return config


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def _run(args: argparse.Namespace) -> int:
    config = _configure(_load_config(args.config), args)
    adapter = None
    if not args.shadow:
        adapter = build_registry_station_adapter(
            config,
            station=int(args.station),
        )
    prober = None
    if args.preflight:
        from free.ip_registry.preflight import PreflightProber

        prober = PreflightProber()
    probe = None
    if args.watchdog:
        probe = HttpStationProbe(base_url=args.watchdog_url)
    runtime = build_qualification(
        config,
        db_path=args.db or None,
        adapter=adapter,
        watchdog_probe=probe,
        preflight_prober=prober,
    )
    if runtime is None:
        print("qualification disabled", file=sys.stderr)
        return 2
    scheduler = build_qualification_scheduler(
        runtime,
        interval_s=args.interval,
        max_hosts=args.max,
        shadow=args.shadow,
        run_preflight=bool(prober is not None),
        run_watchdog=bool(probe is not None),
    )
    try:
        if args.once:
            report = await scheduler.run_once()
            _print(report)
            return 0
        scheduler.start()
        print(
            f"qualification daemon started: station={args.station} "
            f"shadow={args.shadow} interval={args.interval}s "
            f"watchdog={bool(probe)} preflight={bool(prober)}",
            flush=True,
        )
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    finally:
        await scheduler.stop()
        runtime.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--db", default="")
    parser.add_argument("--station", type=int, default=1)
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--interval", type=float, default=900.0)
    parser.add_argument("--shadow", action="store_true", default=True)
    parser.add_argument("--live", dest="shadow", action="store_false")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--watchdog-url", default="http://127.0.0.1:4000")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
