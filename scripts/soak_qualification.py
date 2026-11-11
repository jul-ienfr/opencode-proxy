#!/usr/bin/env python3
"""Short soak smoke for the qualification queue.

Loops claim/release/summary for a bounded duration and checks memory and disk
growth.  The default is a 10 s smoke; a nightly job can pass ``--duration 86400``
for the 24 h soak.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.qualification.domain import Candidate, CandidateStatus  # noqa: E402
from free.qualification.store import QualificationStore  # noqa: E402

DEFAULT_BUDGETS = {
    "peak_mb": 50.0,
    "db_mb": 20.0,
    "min_claims": 100,
}


def _candidate(index: int, now: float) -> Candidate:
    return Candidate(
        hostname=f"soak{index:05d}.nordvpn.com",
        country="Sweden",
        load=float(index % 100),
        transports=("wireguard", "openvpn-udp"),
        potential_score=float(100 - (index % 100)),
        first_seen_at=now,
        last_seen_at=now,
        updated_at=now,
    )


def run(
    duration_s: float,
    candidates: int,
    budgets: dict[str, float],
    *,
    db_path: str | Path | None = None,
    batch_size: int = 50,
    interval_s: float = 0.01,
    progress_s: float = 0.0,
    progress_cb=None,
) -> dict:
    batch_size = max(1, int(batch_size))
    interval_s = max(0.0, float(interval_s))
    progress_s = max(0.0, float(progress_s))
    with ExitStack() as stack:
        if db_path is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="qual-soak-"))
            db = Path(tmp) / "registry.db"
        else:
            db = Path(db_path)
            db.parent.mkdir(parents=True, exist_ok=True)
        store = QualificationStore(db)
        try:
            now = time.time()
            store.upsert_candidates([_candidate(i, now) for i in range(max(1, candidates))], now=now)
            tracemalloc.start()
            start = time.perf_counter()
            last_progress = start
            claims = 0
            cycles = 0
            max_db_bytes = 0
            while time.perf_counter() - start < max(1.0, float(duration_s)):
                for _ in range(batch_size):
                    candidate = store.claim_next(1, now=time.time())
                    if candidate is None:
                        break
                    store.release(
                        candidate.hostname,
                        status=CandidateStatus.PENDING,
                        now=time.time(),
                    )
                    claims += 1
                cycles += 1
                db_bytes = db.stat().st_size
                wal = db.with_name(db.name + "-wal")
                if wal.exists():
                    db_bytes += wal.stat().st_size
                max_db_bytes = max(max_db_bytes, db_bytes)
                if (
                    progress_cb is not None
                    and progress_s > 0
                    and time.perf_counter() - last_progress >= progress_s
                ):
                    progress_cb(
                        {
                            "elapsed_s": round(time.perf_counter() - start, 1),
                            "cycles": cycles,
                            "claims": claims,
                            "db_mb": round(db_bytes / (1024 * 1024), 3),
                        }
                    )
                    last_progress = time.perf_counter()
                time.sleep(interval_s)
            current, peak = tracemalloc.get_traced_memory()
            summary = store.summary()
        finally:
            store.close()
            tracemalloc.stop()

    peak_mb = peak / (1024 * 1024)
    db_mb = max_db_bytes / (1024 * 1024)
    payload = {
        "duration_s": round(time.perf_counter() - start, 2),
        "candidates": candidates,
        "cycles": cycles,
        "claims": claims,
        "current_mb": round(current / (1024 * 1024), 3),
        "peak_mb": round(peak_mb, 3),
        "db_mb": round(db_mb, 3),
        "summary_total": summary["total"],
        "budgets": budgets,
    }
    payload["violations"] = []
    if peak_mb > budgets["peak_mb"]:
        payload["violations"].append("peak_mb")
    if db_mb > budgets["db_mb"]:
        payload["violations"].append("db_mb")
    if claims < budgets["min_claims"]:
        payload["violations"].append("min_claims")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--candidates", type=int, default=2000)
    parser.add_argument("--db", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--interval", type=float, default=0.01)
    parser.add_argument("--progress-s", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def _progress(snapshot):
        print(json.dumps({"progress": snapshot}, sort_keys=True), flush=True)

    payload = run(
        args.duration,
        args.candidates,
        DEFAULT_BUDGETS,
        db_path=args.db or None,
        batch_size=args.batch,
        interval_s=args.interval,
        progress_s=args.progress_s,
        progress_cb=_progress,
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"duration={payload['duration_s']}s cycles={payload['cycles']} "
            f"claims={payload['claims']} peak={payload['peak_mb']}MB "
            f"db={payload['db_mb']}MB"
        )
        if payload["violations"]:
            print(f"VIOLATIONS: {', '.join(payload['violations'])}")
    return 1 if payload["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
