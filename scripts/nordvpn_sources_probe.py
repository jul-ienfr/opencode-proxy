#!/usr/bin/env python3
"""Probe the NordVPN source registry without downloading large archives.

Reads ``docs/nordvpn_sources.yaml`` and performs a lightweight HEAD request on
each HTTP(S) source.  Local ``file:`` entries are checked for existence;
``container:`` entries are reported as skipped because they need a live Docker
daemon.

Usage:
    python scripts/nordvpn_sources_probe.py
    python scripts/nordvpn_sources_probe.py --json
    python scripts/nordvpn_sources_probe.py --only nordvpn_api_servers --only nordcache_packed
    python scripts/nordvpn_sources_probe.py --timeout 15

This script is intentionally not a pytest test: it needs network access and is
meant for operators and nightly jobs, not the default CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "nordvpn_sources.yaml"
USER_AGENT = "opencode-proxy-source-probe/1.0"


def _probe_http(url: str, timeout: float) -> dict:
    started = time.monotonic()
    result: dict = {"url": url, "method": "HEAD"}
    try:
        response = requests.head(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code in {403, 405, 501}:
            result["method"] = "GET"
            response = requests.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                stream=True,
            )
        result["status"] = response.status_code
        result["ok"] = 200 <= response.status_code < 400
        result["content_type"] = response.headers.get("content-type", "")
        result["content_length"] = response.headers.get("content-length")
        result["etag"] = response.headers.get("etag")
        result["last_modified"] = response.headers.get("last-modified")
    except Exception as exc:  # noqa: BLE001 - probe reports any failure
        result["status"] = None
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def _probe_file(path: str) -> dict:
    target = ROOT / path.removeprefix("file:")
    return {
        "url": path,
        "method": "FILE",
        "ok": target.exists(),
        "status": "present" if target.exists() else "missing",
        "path": str(target),
    }


def probe(sources: list[dict], *, only: set[str], timeout: float) -> list[dict]:
    results: list[dict] = []
    for source in sources:
        source_id = source["id"]
        if only and source_id not in only:
            continue
        url = source["url"]
        if url.startswith("file:"):
            result = _probe_file(url)
        elif url.startswith("container:"):
            result = {
                "url": url,
                "method": "CONTAINER",
                "ok": None,
                "status": "skipped",
                "reason": "requires a live Docker daemon",
            }
        else:
            result = _probe_http(url, timeout)
        result["id"] = source_id
        result["name"] = source["name"]
        result["status_manifest"] = source["status"]
        results.append(result)
    return results


def _print_table(results: list[dict]) -> None:
    width = max((len(r["id"]) for r in results), default=4)
    print(f"{'id'.ljust(width)}  {'ok':<5}  {'http':<4}  {'ms':>7}  status")
    print(f"{'-' * width}  {'-' * 5}  {'-' * 4}  {'-' * 7}  {'-' * 6}")
    for r in results:
        ok = "yes" if r.get("ok") else ("skip" if r.get("ok") is None else "NO")
        print(
            f"{r['id'].ljust(width)}  {ok:<5}  {str(r.get('status') or ''):<4}  "
            f"{str(r.get('elapsed_ms', '')):>7}  {r.get('error', r.get('status_manifest', ''))}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=[], help="probe only this source id (repeatable)")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    results = probe(data["sources"], only=set(args.only), timeout=args.timeout)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        _print_table(results)
    return 0 if all(r.get("ok") is not False for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
