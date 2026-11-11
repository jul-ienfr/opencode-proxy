#!/usr/bin/env python3
"""Load simulation for the qualification control plane.

Seeds N hostnames in a temporary registry, runs the requested number of
independent station workers with a deterministic in-process adapter, and
reports the measured 24 h projection.  No tunnel, Docker or network is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.qualification.domain import AttemptOutcome, Candidate  # noqa: E402
from free.qualification.station_runtime import (  # noqa: E402
    ParallelStationRunner,
    StationWorker,
)
from free.qualification.store import QualificationStore  # noqa: E402
from free.qualification.sweep import QualificationSweep, SweepConfig  # noqa: E402


class FastAdapter:
    async def qualify(self, candidate, transport, station):
        index = int(candidate.hostname.rsplit("-", 1)[-1])
        return AttemptOutcome(
            status=200,
            valid=True,
            egress_ip=f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}",
            rtt_ms=10.0 + (index % 50),
        )


def _candidate(index: int, now: float) -> Candidate:
    return Candidate(
        hostname=f"load-{index:05d}",
        country="Sweden",
        transports=("wireguard",),
        potential_score=float(1000 - (index % 1000)),
        rtt_ms=float(10 + (index % 50)),
        load=float(index % 100),
        first_seen_at=now,
        last_seen_at=now,
        updated_at=now,
    )


async def run(hosts: int, workers: int) -> dict:
    hosts = max(1, int(hosts))
    workers = max(1, int(workers))
    now = 1_000_000.0
    per_worker = max(1, (hosts + workers - 1) // workers)
    with tempfile.TemporaryDirectory(prefix="qual-load-") as tmp:
        store = QualificationStore(Path(tmp) / "registry.db", clock=lambda: now)
        try:
            store.upsert_candidates(
                [_candidate(index, now) for index in range(hosts)],
                now=now,
            )
            station_workers = []
            for station in range(1, workers + 1):
                sweep = QualificationSweep(
                    store,
                    FastAdapter(),
                    config=SweepConfig(
                        station=station,
                        shadow=False,
                        transport_rounds=1,
                    ),
                    clock=lambda: now,
                )
                station_workers.append(
                    StationWorker(station, store, sweep, clock=lambda: now)
                )
            started = time.perf_counter()
            report = await ParallelStationRunner(station_workers).run(
                epoch_id="load",
                max_hosts=per_worker,
                shadow=False,
            )
            duration = time.perf_counter() - started
            summary = store.summary()
        finally:
            store.close()
    projected = (hosts / duration) * 86400.0 if duration > 0 else 0.0
    return {
        "hosts": hosts,
        "workers": workers,
        "duration_s": round(duration, 3),
        "claimed": int(summary.get("tested") or 0),
        "usable": int(summary.get("usable") or 0),
        "hosts_per_s": round(hosts / duration, 1) if duration else 0.0,
        "projected_24h_hosts": int(projected),
        "under_24h": projected >= hosts,
        "worker_errors": list(report.errors),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", type=int, default=7070)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = asyncio.run(run(args.hosts, args.workers))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"hosts={payload['hosts']} workers={payload['workers']} "
            f"duration={payload['duration_s']}s "
            f"usable={payload['usable']} "
            f"projected_24h={payload['projected_24h_hosts']}"
        )
    return 0 if payload["under_24h"] and not payload["worker_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
