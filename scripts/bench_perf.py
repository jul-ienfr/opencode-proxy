"""[plan v10 §3.7/§4 Lot 0 t2] Harnais de perf CI — budgets chiffrés mesurables.

Mesure trois proxys des budgets §3.7 (sans daemon Docker, mock pur) :
  1. lock_contention   -> budget "lock global <50ms" (asyncio.Lock sous N tâches)
  2. atomic_write      -> cycle tmp+fsync+replace du pattern shared_rotation/config
  3. sqlite_index_p95  -> requête indexée type requests.db (idx station+ts)

Usage :
  python scripts/bench_perf.py --baseline     # fige logs/bench_baseline.json
  python scripts/bench_perf.py                # mesure + compare si baseline présente
  python scripts/bench_perf.py --json         # sortie machine

Règle §13.5 : régression >20% vs baseline sur un budget -> exit code 2.
Les Lots 2/3/5 brancheront ici les mesures réelles (rotation, soft_rotate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "logs" / "bench_baseline.json"
REGRESSION_THRESHOLD = 0.20  # §13.5


# ── benches ──────────────────────────────────────────────────────────────


def bench_lock_contention(n_tasks: int = 200) -> float:
    """p95 (ms) d'acquisition d'un asyncio.Lock contended — proxy du budget
    `_apply_station_lock` <50ms (§3.7)."""

    async def run() -> float:
        lock = asyncio.Lock()
        samples: list[float] = []

        async def worker() -> None:
            t0 = time.perf_counter()
            async with lock:
                samples.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0)

        await asyncio.gather(*(worker() for _ in range(n_tasks)))
        return statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples)

    return asyncio.run(run())


def bench_atomic_write(iterations: int = 30) -> float:
    """p95 (ms) d'un cycle tmp+fsync+replace — pattern shared_rotation.yaml/
    credentials.env (§3.6.1)."""
    samples: list[float] = []
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "state.json")
        payload = b'{"ip_latency": {}, "schema_version": 2}' * 8
        for _ in range(iterations):
            t0 = time.perf_counter()
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, target)
            samples.append((time.perf_counter() - t0) * 1000)
    return statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples)


def bench_sqlite_index_p95(rows: int = 5000, queries: int = 40) -> float:
    """p95 (ms) d'une requête filtrée par station+ts avec index composé —
    budget 'idx_requests_station_ts utilisé, pas de N+1' (§7)."""
    samples: list[float] = []
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "bench.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, station INT, ts TEXT, duration_ms INT)"
        )
        conn.execute("CREATE INDEX idx_requests_station_ts ON requests(station, ts)")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany(
            "INSERT INTO requests(station, ts, duration_ms) VALUES (?, ?, ?)",
            ((i % 4 + 1, now, 100 + i % 900) for i in range(rows)),
        )
        conn.commit()
        for _ in range(queries):
            t0 = time.perf_counter()
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM requests WHERE station=2 AND ts>?",
                (now,),
            ).fetchall()
            assert any("idx_requests_station_ts" in str(row) for row in plan), (
                "index composé non utilisé — budget §7 violé"
            )
            samples.append((time.perf_counter() - t0) * 1000)
        conn.close()
    return statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples)


BUDGETS = {
    # nom: (fonction, budget_ms §3.7, description)
    "lock_contention_p95_ms": (bench_lock_contention, 50.0, "budget lock global <50ms"),
    "atomic_write_p95_ms": (bench_atomic_write, None, "pattern tmp+fsync+replace (référence)"),
    "sqlite_index_p95_ms": (bench_sqlite_index_p95, None, "requête indexée requests.db (référence)"),
}


def run_benches() -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for name, (fn, budget, desc) in BUDGETS.items():
        value = round(fn(), 3)
        out[name] = {"value": value, "budget": budget, "desc": desc}
    return out


def compare(baseline: dict, current: dict) -> list[str]:
    regressions: list[str] = []
    for name, cur in current.items():
        old = baseline.get(name, {}).get("value")
        if isinstance(old, (int, float)) and old > 0:
            delta = (cur["value"] - old) / old
            # [v10 correctif] plancher absolu : +40% sur 0.005 ms = bruit de
            # mesure (2 microsecondes), pas une régression. Ne signaler que
            # si dégradation relative ET absolue sont significatives.
            MIN_ABS_DELTA_MS = 0.5
            if (
                delta > REGRESSION_THRESHOLD
                and (float(cur["value"]) - old) >= MIN_ABS_DELTA_MS
            ):
                regressions.append(
                    f"{name}: {old} -> {cur['value']} ms "
                    f"(+{delta:.0%} > {REGRESSION_THRESHOLD:.0%})"
                )
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="figer la baseline courante")
    parser.add_argument("--json", action="store_true", help="sortie JSON pure")
    args = parser.parse_args()

    results = run_benches()
    lines: list[str] = []
    for name, r in results.items():
        budget = f" (budget {r['budget']}ms)" if r["budget"] else ""
        flag = "OVER!" if r["budget"] and r["value"] > r["budget"] else "ok"
        lines.append(f"{name:26s} {r['value']:>9.3f} ms {flag}{budget}  — {r['desc']}")

    exit_code = 0
    for name, r in results.items():
        if r["budget"] and r["value"] > r["budget"]:
            print(f"BUDGET DÉPASSÉ: {name} = {r['value']}ms > {r['budget']}ms")
            exit_code = 2

    baseline_data = None
    if BASELINE_PATH.exists():
        try:
            baseline_data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:
            baseline_data = None
    if not args.baseline and baseline_data:
        regs = compare(baseline_data.get("results", {}), results)
        for reg in regs:
            print(f"RÉGRESSION vs baseline ({BASELINE_PATH.name}): {reg}")
        if regs:
            exit_code = 2

    if args.baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps({"results": results}, indent=2), encoding="utf-8"
        )
        lines.append(f"\nBaseline figée -> {BASELINE_PATH}")

    print("\n".join(lines))
    if args.json:
        print(json.dumps(results))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
