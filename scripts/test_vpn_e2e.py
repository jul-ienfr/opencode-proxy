#!/usr/bin/env python3
"""End-to-end VPN smoke tests — instance 4001 ONLY, never a real Claude Code client.

Validates that free-model traffic exits through the gluetun NordVPN tunnel.

Usage:
    OPENCODE_PORT=4001 python opencode.py --no-gui  # instance under test (headless)
    python scripts/test_vpn_e2e.py [--port 4001]   # run the tests

Tests:
  1. test_smoke_non_stream  POST /v1/chat/completions (deepseek-v4-flash) → 200
                            + /api/vpn-status → current_ip set, vpn_status connected
  2. test_smoke_stream      POST /v1/messages (stream: true) → 200 + SSE chunks
  3. test_forced_rotation   POST /api/vpn/next → current_ip changes
  4. test_graceful_failure  docker stop opencode-vpn → clean error (never 500),
                            docker start → reconnection (connected again)
  5. test_no_auth_failed    docker logs opencode-vpn --since 10m → 0 AUTH_FAILED
"""

import argparse
import asyncio
import subprocess
import sys

import httpx

FAKE_KEY = "sk-e2e-test"                      # proxy picks its own key for upstream
CONTAINER = "opencode-vpn"
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED += 1


def docker(args: list[str]) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120)
    return out.stdout.strip()


async def vpn_status(client: httpx.AsyncClient) -> dict:
    r = await client.get("/api/vpn-status")
    r.raise_for_status()
    return r.json()


# ── 1. non-stream smoke ────────────────────────────────────────────

async def test_smoke_non_stream(base: str) -> None:
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 16,
    }
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        r = await client.post("/v1/chat/completions", json=payload,
                              headers={"Authorization": f"Bearer {FAKE_KEY}"})
        check("non-stream → HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
        st = await vpn_status(client)
    check("vpn connected", st.get("vpn_status") == "connected", st.get("vpn_status", ""))
    check("current_ip set", bool(st.get("current_ip")), st.get("current_ip", ""))


# ── 2. stream smoke ────────────────────────────────────────────────

async def test_smoke_stream(base: str) -> None:
    payload = {
        "model": "deepseek-v4-flash",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "Count to 3"}],
    }
    chunks = 0
    async with httpx.AsyncClient(base_url=base, timeout=180) as client:
        async with client.stream(
            "POST", "/v1/messages", json=payload,
            headers={"x-api-key": FAKE_KEY, "anthropic-version": "2023-06-01"},
        ) as r:
            check("stream → HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
            async for line in r.aiter_lines():
                if line.startswith("data:") and line.strip() != "data: [DONE]":
                    chunks += 1
    check("stream → SSE chunks", chunks > 0, f"{chunks} chunks")


# ── 3. forced rotation ─────────────────────────────────────────────

async def test_forced_rotation(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=180) as client:
        before = (await vpn_status(client)).get("current_ip")
        r = await client.post("/api/vpn/next")
        r.raise_for_status()
        body = r.json()
        after = (await vpn_status(client)).get("current_ip")
    check("rotation ok", body.get("ok") is True)
    check("IP changed", bool(after) and after != before, f"{before} → {after}")


# ── 4. graceful failure + reconnection ─────────────────────────────

async def test_graceful_failure(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        docker(["stop", CONTAINER])
        try:
            r = await client.post(
                "/v1/chat/completions",
                json={"model": "deepseek-v4-flash",
                      "messages": [{"role": "user", "content": "Say OK"}],
                      "max_tokens": 16},
                headers={"Authorization": f"Bearer {FAKE_KEY}"},
            )
            check("clean error (not 500)", r.status_code != 500, f"HTTP {r.status_code}")
        finally:
            docker(["start", CONTAINER])
        # wait for reconnection (up to 120 s)
        st = {}
        for _ in range(60):
            try:
                st = await vpn_status(client)
                if st.get("vpn_status") == "connected":
                    break
            except Exception:
                pass
            await asyncio.sleep(2)
    check("reconnected", st.get("vpn_status") == "connected", st.get("vpn_status", ""))


# ── 5. no auth failures ────────────────────────────────────────────

def test_no_auth_failed() -> None:
    logs = docker(["logs", CONTAINER, "--since", "10m"])
    n = logs.count("AUTH_FAILED")
    check("no AUTH_FAILED (10m)", n == 0, f"{n} occurrences")


async def main() -> None:
    parser = argparse.ArgumentParser(description="VPN e2e smoke tests (instance 4001 only)")
    parser.add_argument("--port", type=int, default=4001)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print(f"target: {base}  container: {CONTAINER}")
    for test in (test_smoke_non_stream, test_smoke_stream,
                 test_forced_rotation, test_graceful_failure, test_no_auth_failed):
        print(f"\n── {test.__name__}", flush=True)
        try:
            await test(base) if test is not test_no_auth_failed else test()
        except Exception as e:
            check(test.__name__, False, f"exception: {e}")

    print(f"\n{'ALL PASS' if FAILED == 0 else f'{FAILED} FAILED'}")
    sys.exit(0 if FAILED == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
