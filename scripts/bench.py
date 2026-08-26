#!/usr/bin/env python3
"""
Bench ultra-rapide opencode-proxy
Usage:
  python scripts/bench.py --url http://localhost:4000 --concurrency 50 --requests 1000
  python scripts/bench.py --url http://localhost:4000 --stream  # test SSE
"""

import argparse
import asyncio
import statistics
import time

import httpx


async def bench_health(url, conc, total):
    async with httpx.AsyncClient(timeout=10) as c:
        start = time.perf_counter()
        lat = []

        async def one():
            t0 = time.perf_counter()
            r = await c.get(f"{url}/health")
            lat.append((time.perf_counter() - t0) * 1000)
            return r.status_code

        # run in batches
        for i in range(0, total, conc):
            batch = min(conc, total - i)
            await asyncio.gather(*[one() for _ in range(batch)])
        elapsed = time.perf_counter() - start
        print(f"health {total} reqs conc={conc} in {elapsed:.2f}s = {total / elapsed:.0f} rps")
        print(
            f"p50 {statistics.median(lat):.1f}ms p95 {sorted(lat)[int(len(lat) * 0.95)]:.1f}ms p99 {sorted(lat)[int(len(lat) * 0.99)]:.1f}ms"
        )


async def bench_stream(url):
    body = {
        "model": "mimo-v2.5",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
        "max_tokens": 32,
    }
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("POST", f"{url}/v1/messages", json=body) as r:
            print(f"stream status {r.status_code} headers {dict(r.headers)}")
            ttfb = None
            async for line in r.aiter_lines():
                if ttfb is None:
                    ttfb = (time.perf_counter() - t0) * 1000
                    print(f"TTFB {ttfb:.0f}ms line={line[:80]}")
                if "data: [DONE]" in line:
                    break
            print(f"done in {(time.perf_counter() - t0) * 1000:.0f}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:4000")
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--requests", type=int, default=1000)
    ap.add_argument("--stream", action="store_true")
    args = ap.parse_args()
    if args.stream:
        asyncio.run(bench_stream(args.url))
    else:
        asyncio.run(bench_health(args.url, args.concurrency, args.requests))
