#!/usr/bin/env python3
"""End-to-end VPN smoke tests — instance 4001 ONLY, never a real Claude Code client.

Validates that free-model traffic exits through the gluetun NordVPN tunnel.

Usage:
    OPENCODE_PORT=4001 python opencode.py --no-gui  # instance under test (headless)
    python scripts/vpn_e2e_smoke.py [--port 4001]  # run the tests

    # opt-in deep test: exercises the quota→rotation machinery end-to-end
    # (the exact connect_next chain a 429 triggers via on_quota_exhausted).
    # Needs the dashboard token (DASHBOARD_TOKEN env, .env, or --token).
    python scripts/vpn_e2e_smoke.py --quota-trigger

Tests:
  1. test_smoke_non_stream  POST /v1/chat/completions (deepseek-v4-flash) → 200
                            + /api/vpn-status → current_ip set, vpn_status connected
  2. test_smoke_stream      POST /v1/messages (stream: true) → 200 + SSE chunks
  3. test_forced_rotation   POST /api/vpn/next → IP changed, total_switches
                            incremented, identity index advanced when identity
                            rotation is on (Partie A: IP rotation advances
                            identity), circuit_breaker state well-formed
  4. test_graceful_failure  docker stop opencode-vpn → health-check answers
                            clean JSON (never 500), free request never 500
                            (fail-open direct success or structured JSON
                            error), docker start → reconnected
  5. test_no_auth_failed    docker logs opencode-vpn --since 10m → 0 AUTH_FAILED
  6. test_quota_rotation    [--quota-trigger] temporarily sets quota_per_ip=12
                            (switch fires at 2 requests), makes 2 free requests,
                            asserts the rotation machinery fired, restores the
                            quota in a finally block
  7. test_update_contract   POST /api/vpn/update → clean JSON (never 500):
                            applied, deferred, or "no update available" — the
                            same guard path rollback goes through on failure
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys

import httpx

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

FAKE_KEY = "sk-e2e-test"  # proxy picks its own key for upstream
CONTAINER = "opencode-vpn"
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED += 1


def docker(args: list[str]) -> str:
    out = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=_CREATE_NO_WINDOW,
    )
    return out.stdout.strip()


def load_env_token() -> str:
    """Dashboard token: --token > DASHBOARD_TOKEN env > .env file (test host)."""
    token = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DASHBOARD_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


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
        r = await client.post(
            "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {FAKE_KEY}"}
        )
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
            "POST",
            "/v1/messages",
            json=payload,
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
        before = await vpn_status(client)
        before_ip = before.get("current_ip")
        before_switches = before.get("vpn", {}).get("total_switches", 0)
        before_identity = before.get("vpn", {}).get("identity_index")
        cb = before.get("vpn", {}).get("circuit_breaker", {})
        r = await client.post("/api/vpn/next")
        r.raise_for_status()
        body = r.json()
        after = await vpn_status(client)
        after_ip = after.get("current_ip")
        after_switches = after.get("vpn", {}).get("total_switches", 0)
        after_identity = after.get("vpn", {}).get("identity_index")
        cfg = (await client.get("/api/vpn-config")).json()
    check("rotation ok", body.get("ok") is True)
    check("IP changed", bool(after_ip) and after_ip != before_ip, f"{before_ip} → {after_ip}")
    check(
        "total_switches incremented",
        after_switches > before_switches,
        f"{before_switches} → {after_switches}",
    )
    # Partie A invariant: a successful rotation advances the identity index
    # when identity rotation is on with >1 profile; otherwise it stays put.
    rot_on = bool(cfg.get("identity_rotation")) and len(cfg.get("identity_profiles") or []) > 1
    if rot_on:
        check(
            "identity advanced with rotation",
            after_identity is not None and after_identity != before_identity,
            f"{before_identity} → {after_identity}",
        )
    else:
        check(
            "identity stable (rotation off)",
            after_identity == before_identity,
            f"{before_identity} → {after_identity}",
        )
    check(
        "circuit_breaker well-formed",
        cb.get("state") in ("closed", "open", "half_open"),
        str(cb.get("state")),
    )


# ── 4. graceful failure + reconnection ─────────────────────────────


async def test_graceful_failure(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        docker(["stop", CONTAINER])
        try:
            # Deterministic probe: with the container stopped the SOCKS5
            # tunnel is dead, so the health check must answer clean JSON
            # with ok=False — never a 500.
            hc = await client.post("/api/vpn/health-check")
            try:
                hc_body = hc.json()
                hc_ok = isinstance(hc_body, dict) and hc_body.get("ok") is False
            except ValueError:
                hc_ok, hc_body = False, {}
            check(
                "outage: health-check clean JSON (ok=false)",
                hc.status_code == 200 and hc_ok,
                f"HTTP {hc.status_code}: {hc_body}",
            )

            # Request path: fail-open by design — either a completion (direct
            # path, or the rotation self-healed the container via docker
            # restart) or a structured JSON error. Never a 500.
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 16,
                },
                headers={"Authorization": f"Bearer {FAKE_KEY}"},
            )
            check("outage: request never 500", r.status_code != 500, f"HTTP {r.status_code}")
            if r.status_code == 200:
                try:
                    b = r.json()
                    ok = bool(b.get("choices") or b.get("content") or b.get("output_text"))
                except ValueError:
                    ok = False
                check(
                    "outage: fail-open answer (direct or self-healed)",
                    ok,
                    r.text[:120] if not ok else "",
                )
            else:
                try:
                    b = r.json()
                    ok = isinstance(b, dict) and bool(b.get("error") or b.get("message"))
                except ValueError:
                    ok = False
                check(
                    "outage: structured JSON error", ok, f"HTTP {r.status_code}: {r.text[:120]!r}"
                )
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


# ── 6. quota → rotation (opt-in, needs dashboard token) ─────────────


async def test_quota_rotation(base: str, token: str) -> None:
    """Proactive-quota rotation — the same connect_next chain a 429 triggers
    via on_quota_exhausted → switch_ip. With quota_per_ip=12 the switch fires
    at request #2 (quota - 10). The quota is restored in a finally block."""
    headers = {"X-Dashboard-Token": token}
    async with httpx.AsyncClient(base_url=base, timeout=180) as client:
        cfg = (await client.get("/api/vpn-config")).json()
        original_quota = cfg.get("quota_per_ip")
        if original_quota is None:
            check("quota rotation", False, "quota_per_ip missing from /api/vpn-config")
            return
        try:
            r = await client.post("/api/vpn-config", json={"quota_per_ip": 12}, headers=headers)
            body = r.json() if r.status_code == 200 else {}
            if r.status_code != 200 or not body.get("ok"):
                check(
                    "quota rotation",
                    False,
                    f"quota set rejected: HTTP {r.status_code} {r.text[:100]!r}",
                )
                return
            before = await vpn_status(client)
            before_switches = before.get("vpn", {}).get("total_switches", 0)
            for i in range(2):
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "deepseek-v4-flash",
                        "messages": [{"role": "user", "content": "Say OK"}],
                        "max_tokens": 16,
                    },
                    headers={"Authorization": f"Bearer {FAKE_KEY}"},
                )
                if resp.status_code != 200:
                    check("quota rotation", False, f"free request {i + 1} HTTP {resp.status_code}")
                    return
            # the switch is awaited in the request path; poll as belt-and-braces
            switched = False
            detail = ""
            for _ in range(30):
                st = await vpn_status(client)
                if st.get("vpn", {}).get("total_switches", 0) > before_switches:
                    switched = True
                    detail = f"{before_switches} → {st['vpn']['total_switches']}"
                    break
                await asyncio.sleep(2)
            check("quota rotation fired", switched, detail)
        finally:
            rr = await client.post(
                "/api/vpn-config", json={"quota_per_ip": original_quota}, headers=headers
            )
            if rr.status_code != 200:
                check("quota restored", False, f"HTTP {rr.status_code}")


# ── 7. update endpoint contract ────────────────────────────────────


async def test_update_contract(base: str) -> None:
    """POST /api/vpn/update must answer clean JSON — applied, deferred, or
    "no update available" — the same guard path a failed apply's rollback
    goes through. Never a 500."""
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        r = await client.post("/api/vpn/update")
        try:
            body = r.json()
            ok = isinstance(body, dict) and ("ok" in body or "error" in body)
        except ValueError:
            ok = False
        check(
            "update endpoint clean JSON",
            r.status_code == 200 and ok,
            f"HTTP {r.status_code}: {r.text[:120]!r}",
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="VPN e2e smoke tests (instance 4001 only)")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument(
        "--token", default="", help="dashboard token (else DASHBOARD_TOKEN env/.env)"
    )
    parser.add_argument(
        "--quota-trigger",
        action="store_true",
        help="run test_quota_rotation (temporarily lowers quota_per_ip)",
    )
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print(f"target: {base}  container: {CONTAINER}")
    tests: list = [
        test_smoke_non_stream,
        test_smoke_stream,
        test_forced_rotation,
        test_graceful_failure,
        test_no_auth_failed,
        test_update_contract,
    ]
    token = args.token or load_env_token()
    if args.quota_trigger:
        if not token:
            check(
                "quota rotation",
                False,
                "--quota-trigger requires a dashboard token (--token / DASHBOARD_TOKEN)",
            )
        else:
            tests.append(test_quota_rotation)
    for test in tests:
        print(f"\n── {test.__name__}", flush=True)
        try:
            if test is test_no_auth_failed:
                test()
            elif test is test_quota_rotation:
                await test(base, token)
            else:
                await test(base)
        except Exception as e:
            check(test.__name__, False, f"exception: {e}")

    print(f"\n{'ALL PASS' if FAILED == 0 else f'{FAILED} FAILED'}")
    sys.exit(0 if FAILED == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
