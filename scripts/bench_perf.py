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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # import opencode / protocol_mapping / dashboard
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


# ── [audit perf 26/08] budgets hot-path (Phase 0 du plan) ───────────────

_OC = None  # module opencode importé une seule fois


def _oc():
    global _OC
    if _OC is None:
        sys.path.insert(0, str(ROOT))
        import opencode as _m

        _OC = _m
    return _OC


def bench_static_middleware_overhead(iterations: int = 500) -> float:
    """p95 (ms) d'une requête ASGI synthétique à travers _StaticCacheMiddleware
    (pur ASGI, P1.3) — budget <0.1 ms/requête (l'ancien BaseHTTPMiddleware :
    ~1-8 ms). Le hit gzip sert les octets pré-comprimés sans I/O disque."""

    async def run() -> float:
        import asyncio as _aio

        from dashboard.api import _precompress_static_assets, _StaticCacheMiddleware

        async def nop_app(scope, receive, send):  # ne doit PAS être atteint sur un hit
            raise AssertionError("hit gzip doit court-circuiter l'app")

        static_dir = ROOT / "static"
        pre = _precompress_static_assets(str(static_dir))
        if not pre:
            # pas d'assets (arbre partiel) : mesure du chemin pass-through
            pre = {}
        mw = _StaticCacheMiddleware(nop_app, precompressed=pre)
        path = next(iter(pre), "/__no_asset__")
        scope_tpl = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [(b"accept-encoding", b"gzip")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        samples: list[float] = []
        for _ in range(iterations):
            sent: list[dict] = []

            async def send(message):
                sent.append(message)

            scope = dict(scope_tpl)
            t0 = time.perf_counter()
            await mw(scope, receive, send)
            samples.append((time.perf_counter() - t0) * 1000)
            await _aio.sleep(0)
        return statistics.quantiles(samples, n=20)[18]

    return asyncio.run(run())


def bench_sse_pump(chunks: int = 4000) -> float:
    """µs par chunk drainés par la pompe SSE (_sse_coalesce, P4.5) sur un
    générateur mémoire — référence pompe d'émission (tasks/s implicites)."""

    async def run() -> float:
        oc = _oc()

        async def gen():
            for _ in range(chunks):
                yield b'data: {"delta": "x"}\n\n'

        n = 0
        t0 = time.perf_counter()
        async for _group in oc._sse_coalesce(gen(), max_group_bytes=65536):
            n += len(_group)
        elapsed = time.perf_counter() - t0
        if elapsed <= 0 or n == 0:
            return 9999.0
        return (elapsed / chunks) * 1_000_000  # µs/chunk

    return asyncio.run(run())


def bench_conversion_cache(iterations: int = 300) -> dict[str, float]:
    """p95 (ms) hit vs miss du cache de conversion anthropic→openai
    (blake2b, LRU 512) — référence C1."""
    import protocol_mapping as pm

    body_tpl = {
        "model": "glm-5.1",
        "system": "You are a helpful assistant. " * 40,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello " * 60}]},
        ],
        "max_tokens": 512,
    }

    def _body(i: int) -> dict:
        b = json.loads(json.dumps(body_tpl))
        b["messages"][0]["content"][0]["text"] += str(i)  # miss : corps unique
        return b

    # warmup
    pm.anthropic_to_openai(body_tpl, "glm-5.1")
    hits: list[float] = []
    misses: list[float] = []
    for i in range(iterations):
        t0 = time.perf_counter()
        pm.anthropic_to_openai(body_tpl, "glm-5.1", raw=b"stable-bytes")
        hits.append((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        pm.anthropic_to_openai(_body(i), "glm-5.1")
        misses.append((time.perf_counter() - t1) * 1000)
    q = lambda xs: statistics.quantiles(xs, n=20)[18]  # noqa: E731
    return {"conv_hit_p95_ms": round(q(hits), 4), "conv_miss_p95_ms": round(q(misses), 4)}


def bench_free_usage_enqueue(iterations: int = 200) -> float:
    """p95 (ms) de _log_free_model_usage APRÈS P1.2 = put_nowait queue writer.
    Détecteur anti-régression : si du SQLite repasse sur la boucle (execute/
    commit synchrone), la valeur explose (>budget)."""
    oc = _oc()

    class _BoomConn:
        def execute(self, *a, **k):
            raise AssertionError("sqlite.execute sur la boucle (P1.2 violé)")

        def commit(self):
            raise AssertionError("sqlite.commit sur la boucle (P1.2 violé)")

    saved_conn = getattr(oc, "_conn", None)
    oc._conn = _BoomConn()
    try:
        samples: list[float] = []
        for i in range(iterations):

            def _one(idx=i):
                t0 = time.perf_counter()
                oc._log_free_model_usage(
                    "claude-sonnet",
                    "glm-5.1",
                    f"sk-bench-{idx:04d}",
                    "wrk_bench",
                    200,
                    tokens_in=10,
                    tokens_out=5,
                    duration_ms=42,
                    ip="192.0.2.1",
                )
                return (time.perf_counter() - t0) * 1000

            samples.append(_one())
        return statistics.quantiles(samples, n=20)[18]
    finally:
        oc._conn = saved_conn


def bench_curl_pool_checkout(concurrency: int = 60, rounds: int = 3) -> float:
    """p95 (ms) d'un checkout/checkin sur le pool curl (M=3, contended) —
    référence A1 ; le chemin attendu reste <<1ms hors saturation réelle."""

    async def run() -> float:
        oc = _oc()

        class _FakeSess:
            async def close(self):
                pass

        pool = oc._CurlSessionPool(3)
        lat: list[float] = []

        async def _worker():
            for _ in range(rounds):
                t0 = time.perf_counter()
                slot = await pool.checkout(lambda: _FakeSess())
                lat.append((time.perf_counter() - t0) * 1000)
                await pool.checkin(slot)

        await asyncio.gather(*(_worker() for _ in range(concurrency)))
        if not lat:
            return 9999.0
        return statistics.quantiles(lat, n=20)[18] if len(lat) >= 20 else max(lat)

    return asyncio.run(run())


HOT_PATH_BUDGETS = {
    "static_mw_p95_ms": (
        bench_static_middleware_overhead,
        0.1,
        "P1.3 middleware statique pur ASGI <0.1ms",
    ),
    "sse_pump_us_per_chunk": (
        bench_sse_pump,
        None,
        "pompe SSE coalesce (référence P4.5)",
    ),
    "free_usage_enq_p95_ms": (
        bench_free_usage_enqueue,
        0.5,
        "P1.2 enqueue usage sans sqlite sur la boucle",
    ),
    "curl_pool_checkout_p95_ms": (
        bench_curl_pool_checkout,
        None,
        "checkout/checkin pool curl M=3 (référence A1)",
    ),
}


def run_benches() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, object]] = {}
    for name, (fn, budget, desc) in BUDGETS.items():
        value = round(fn(), 3)
        out[name] = {"value": value, "budget": budget, "desc": desc}
    for name, r in bench_conversion_cache().items():
        out[name] = {"value": float(r), "budget": None, "desc": "cache conversion hit/miss (référence)"}
    for name, (fn, budget, desc) in HOT_PATH_BUDGETS.items():
        try:
            value = round(fn(), 3)
        except Exception as e:
            out[name] = {"value": 9999.0, "budget": budget, "desc": f"{desc} — ERREUR: {e}"}
            continue
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

    results: dict[str, dict[str, Any]] = run_benches()
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
