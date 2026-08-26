"""Diagnostic isolé : pourquoi curl_cffi timeout 60s via le proxy gluetun
alors que curl GNU passe. Instance 4001 only — aucun vrai client.
Teste socks5://127.0.0.1:1080 et http://127.0.0.1:8888, 2 URLs, 2 impersonations.
Timeout court (12s) pour itérer vite.
"""

import asyncio
import time

from curl_cffi.requests import AsyncSession

URLS = {
    "ipify": "https://api.ipify.org",
    "opencode-free": "https://opencode.ai/zen/v1/chat/completions",
}
PROXIES = ["socks5://127.0.0.1:1080", "http://127.0.0.1:8888"]
IMPERS = [None, "chrome131"]


async def attempt(name: str, proxy: str, imp: str, url: str) -> str:
    t0 = time.monotonic()
    try:
        async with AsyncSession(impersonate=imp, proxy=proxy) as s:
            resp = await s.post(
                url,
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=12,
            )
        dt = time.monotonic() - t0
        body = resp.text[:120].replace("\n", " ")
        return f"OK   {dt:6.1f}s status={resp.status_code} body={body!r}"
    except Exception as e:
        dt = time.monotonic() - t0
        msg = str(e).splitlines()[0][:150]
        return f"FAIL {dt:6.1f}s {type(e).__name__}: {msg}"


async def main():
    print(f"curl_cffi version: {__import__('curl_cffi').__version__}")
    print("-" * 100)
    tasks = []
    for proxy in PROXIES:
        for imp in IMPERS:
            for name, url in URLS.items():
                label = f"{proxy:28s} | {imp or 'none':10s} | {name}"
                tasks.append((label, asyncio.create_task(attempt(label, proxy, imp, url))))
    results = await asyncio.gather(*[t for _, t in tasks])
    for (label, _), res in zip(tasks, results, strict=False):
        print(f"{label} -> {res}")


if __name__ == "__main__":
    asyncio.run(main())
