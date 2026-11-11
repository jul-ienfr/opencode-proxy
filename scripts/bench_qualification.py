#!/usr/bin/env python3
"""Deterministic benchmark for the qualification hot paths.

It measures catalog upsert, potential scoring, claim ordering and summary.  The
budgets are intentionally conservative; the script exits non-zero when a
budget is exceeded so it can be used as a nightly gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.qualification.domain import Candidate  # noqa: E402
from free.qualification.scoring import PotentialWeights, potential_score  # noqa: E402
from free.qualification.store import QualificationStore  # noqa: E402

DEFAULT_CANDIDATES = 5000
DEFAULT_BUDGETS = {
    "upsert_per_item_ms": 2.0,
    "score_p95_ms": 0.5,
    "claim_p95_ms": 20.0,
    "summary_ms": 100.0,
}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return float(ordered[index])


def _candidate(index: int, now: float) -> Candidate:
    return Candidate(
        hostname=f"host{index:05d}.nordvpn.com",
        country="Sweden",
        city="Stockholm",
        load=float(index % 100),
        transports=("wireguard", "openvpn-udp", "openvpn-tcp"),
        potential_score=float(100 - (index % 100)),
        rtt_ms=float(5 + (index % 300)),
        quota_remaining=500 - (index % 100),
        first_seen_at=now,
        last_seen_at=now,
        updated_at=now,
    )


def run(candidates: int, budgets: dict[str, float]) -> dict:
    now = 1_000_000.0
    rows = [_candidate(index, now) for index in range(max(1, int(candidates)))]
    with tempfile.TemporaryDirectory(prefix="qual-bench-") as tmp:
        store = QualificationStore(Path(tmp) / "registry.db", clock=lambda: now)
        try:
            started = time.perf_counter()
            store.upsert_candidates(rows, now=now)
            upsert_s = time.perf_counter() - started

            weights = PotentialWeights()
            score_times: list[float] = []
            for candidate in rows:
                started = time.perf_counter()
                potential_score(candidate, now=now, weights=weights)
                score_times.append((time.perf_counter() - started) * 1000.0)

            claim_times: list[float] = []
            claimed = 0
            while True:
                started = time.perf_counter()
                candidate = store.claim_next(1, now=now, epoch_id="bench")
                claim_times.append((time.perf_counter() - started) * 1000.0)
                if candidate is None:
                    break
                claimed += 1

            started = time.perf_counter()
            summary = store.summary()
            summary_ms = (time.perf_counter() - started) * 1000.0
        finally:
            store.close()

    upsert_per_item_ms = (upsert_s * 1000.0) / max(1, len(rows))
    payload = {
        "candidates": len(rows),
        "claimed": claimed,
        "upsert_total_ms": round(upsert_s * 1000.0, 3),
        "upsert_per_item_ms": round(upsert_per_item_ms, 4),
        "score_p50_ms": round(_percentile(score_times, 50), 5),
        "score_p95_ms": round(_percentile(score_times, 95), 5),
        "claim_p50_ms": round(_percentile(claim_times, 50), 4),
        "claim_p95_ms": round(_percentile(claim_times, 95), 4),
        "summary_ms": round(summary_ms, 4),
        "summary_total": summary["total"],
        "budgets": budgets,
    }
    payload["violations"] = [
        key
        for key, limit in budgets.items()
        if float(payload.get(key, 0.0)) > float(limit)
    ]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=int, default=DEFAULT_CANDIDATES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = run(args.candidates, DEFAULT_BUDGETS)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"candidates={payload['candidates']} claimed={payload['claimed']} "
            f"upsert/item={payload['upsert_per_item_ms']:.3f}ms "
            f"score/p95={payload['score_p95_ms']:.4f}ms "
            f"claim/p95={payload['claim_p95_ms']:.2f}ms "
            f"summary={payload['summary_ms']:.2f}ms"
        )
        if payload["violations"]:
            print(f"BUDGET VIOLATIONS: {', '.join(payload['violations'])}")
    return 1 if payload["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
