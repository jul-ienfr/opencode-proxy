#!/usr/bin/env python3
"""Pin a NordVPN country and find a free-quota-fresh egress (real 200).

The free quota is bucketed by the upstream-visible IP.  A country pin gives
a random server inside that country, and only some of them still have a
fresh bucket.  This script probes the current egress first (no pin if it is
already good), then pins the requested country and retries until the
official free body returns 200.

Usage:
    python scripts/nordvpn_free_pin.py                 # Albania, 5 attempts
    python scripts/nordvpn_free_pin.py --country Iceland --attempts 8
    python scripts/nordvpn_free_pin.py --proxy-url http://127.0.0.1:4000

On success it clears the stale quota entries for the found IP/model and
refreshes the manager's current IP, so the proxy's VPN leg serves it
immediately.  Requires the proxy to be running and `proxy_mode` = vpn.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

import opencode as oc  # noqa: E402
from free.identities import IpIdentity  # noqa: E402

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
SOCKS = "socks5h://127.0.0.1:1080"
MODEL = "deepseek-v4.1-flash-free"


def _api(base: str, path: str, payload: dict, timeout: float = 120.0):
    try:
        return requests.post(base + path, json=payload, timeout=timeout)
    except Exception as exc:  # pragma: no cover - operational script
        print(f"  API {path} failed: {type(exc).__name__}: {exc}")
        return None


def _egress() -> str:
    try:
        return curl_requests.get(
            "https://api.ipify.org",
            proxies={"https": SOCKS, "http": SOCKS},
            timeout=20,
            impersonate="chrome",
        ).text.strip()
    except Exception:
        return ""


def _probe(ip: str) -> tuple[int, str, str]:
    with open(ROOT / "free" / "official_payload.json", encoding="utf-8") as f:
        body = json.load(f)["body"]
    body["model"] = MODEL
    ident = IpIdentity(
        f"nord-{ip}",
        base_project="global",
        forced_client="desktop",
        forced_version="1.19.5",
        generation=1,
        persist=False,
    )
    headers = ident.official_provider_headers(ENDPOINT, None)
    headers.update(
        {
            "Connection": "keep-alive",
            "Accept": "*/*",
            "Host": "opencode.ai",
            "Accept-Encoding": "gzip, deflate, br, zstd",
        }
    )
    imp, fp = oc._get_free_fp_kwargs("1.19.5")
    try:
        resp = curl_requests.post(
            ENDPOINT,
            headers=headers,
            json=body,
            proxies={"https": SOCKS, "http": SOCKS},
            timeout=90,
            **({"impersonate": imp} if imp else {}),
            **fp,
        )
        return (
            resp.status_code,
            resp.headers.get("retry-after") or "",
            (resp.text or "")[:160].replace("\n", " "),
        )
    except Exception as exc:  # pragma: no cover - operational script
        return 0, type(exc).__name__, str(exc)[:160]


def _wait_egress(previous: str, timeout: float = 90.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = _egress()
        if ip and ip != previous:
            return ip
        time.sleep(3)
    return _egress()


def _bless(base: str, ip: str) -> None:
    """Make the proxy serve the found egress: ledger + manager current IP."""
    _api(base, "/api/free/quota/clear", {"ip": ip})
    _api(base, "/api/free/quota/clear", {"model": MODEL})
    _api(base, "/api/vpn/health-check", {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="Albania")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--station", type=int, default=1)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    args = parser.parse_args()

    base = args.proxy_url.rstrip("/")
    _api(base, "/api/vpn-config", {"egress_allow_any_country": True})
    _api(base, "/api/vpn/rotation-paused", {"paused": True})

    ip = _egress()
    if ip:
        status, retry_after, body = _probe(ip)
        print(f"current egress {ip} -> {status} (Retry-After={retry_after or '-'})")
        if status == 200:
            _bless(base, ip)
            print(f"OK: {ip} already serves the free model — no pin needed.")
            return 0

    for attempt in range(1, max(1, args.attempts) + 1):
        previous = _egress()
        resp = _api(base, f"/api/vpn/station/{args.station}/pin_country", {"country": args.country})
        if resp is None or resp.status_code != 200:
            print(f"attempt {attempt}: pin refused: {getattr(resp, 'text', '')[:160]}")
            continue
        ip = _wait_egress(previous)
        if not ip:
            print(f"attempt {attempt}: no egress after pin")
            continue
        status, retry_after, body = _probe(ip)
        print(f"attempt {attempt}: {ip} -> {status} (Retry-After={retry_after or '-'})")
        if status == 200:
            _bless(base, ip)
            print(f"OK: found fresh NordVPN egress {ip} ({args.country}).")
            return 0

    print("No fresh egress found — try another country or more attempts.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
