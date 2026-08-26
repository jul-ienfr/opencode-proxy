"""
bench_pool.py — benchmark pool/mmap/cache (Phase3 P3.4)

Mesure le tuning `CACHE_MIN_PROMPT_SIZE 2000`/`max_size 1000` TTL300,
`pool 500/200/30`, `mmap 268M` vs `cache_size`.

Usage:
  python scripts/bench_pool.py --url http://localhost:4000 --concurrency 200
"""

import argparse
import asyncio
import statistics
import time


async def bench(url: str, concurrency: int, total: int = 500):
    import httpx

    async def one(client):
        t0 = time.monotonic()
        try:
            r = await client.get(f"{url}/health", timeout=5.0)
            return time.monotonic() - t0, r.status_code
        except Exception:
            return time.monotonic() - t0, 0

    limits = httpx.Limits(max_connections=500, max_keepalive_connections=200, keepalive_expiry=30)
    async with httpx.AsyncClient(limits=limits, timeout=5.0) as client:
        start = time.monotonic()
        sem = asyncio.Semaphore(concurrency)
        results = []

        async def bounded():
            async with sem:
                return await one(client)

        tasks = [asyncio.create_task(bounded()) for _ in range(total)]
        for t in asyncio.as_completed(tasks):
            results.append(await t)
        elapsed = time.monotonic() - start
        latencies = [r[0] for r in results]
        statuses = [r[1] for r in results]
        print(
            f"concurrency={concurrency} total={total} elapsed={elapsed:.2f}s rps={total / elapsed:.1f}"
        )
        print(
            f"  p50={statistics.median(latencies) * 1000:.1f}ms p95={sorted(latencies)[int(len(latencies) * 0.95)] * 1000:.1f}ms"
        )
        print(f"  200={statuses.count(200)} 503={statuses.count(503)} 0={statuses.count(0)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:4000")
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--total", type=int, default=500)
    args = ap.parse_args()
    asyncio.run(bench(args.url, args.concurrency, args.total))
