#!/usr/bin/env python3
"""Teste un pool de proxies HTTP/HTTPS/SOCKS5 avec la capture free 2.2.5.

Pour chaque proxy :
1. lit l'egress via https://api.ipify.org ;
2. envoie la capture `free/captures/2.2.5.json` avec un `ses_` neuf ;
3. classe 200 / 429 / dead / duplicate.

Les proxies publics sont souvent lents ou flagged ; le but est de trouver des
egress 200 sans rebinder les stations. Aucun secret n'est envoyé : uniquement
`Authorization: Bearer public` et la capture de test.

Exemples :
    python scripts/test_proxy_pool.py --proxies-file proxies.txt --limit 50
    python scripts/test_proxy_pool.py --url https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt --limit 30
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import opencode as oc  # noqa: E402
from curl_cffi import requests  # noqa: E402

from test_free_egress import CAPTURE, load_capture, post_once  # noqa: E402

IPIFY = "https://api.ipify.org"
OUT = ROOT / "logs" / "proxy_pool_results.json"


def normalize_proxy(line: str) -> str | None:
    value = line.strip()
    if not value or value.startswith("#"):
        return None
    if "://" not in value:
        value = "http://" + value
    scheme = value.split("://", 1)[0].lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        return None
    return value


def fetch_lines(url: str) -> list[str]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8", "replace").splitlines()


def egress_ip(proxy: str, timeout: float, impersonate: str | None, fp: dict | None):
    kwargs = {
        "proxies": {"http": proxy, "https": proxy},
        "timeout": timeout,
        "stream": False,
    }
    if impersonate:
        kwargs["impersonate"] = impersonate
    if isinstance(fp, dict):
        kwargs.update(fp)
    response = requests.get(IPIFY, **kwargs)
    text = (response.text or "").strip()
    if response.status_code == 200 and text.count(".") == 3:
        return text
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxies-file", help="fichier, un proxy par ligne")
    parser.add_argument("--url", action="append", default=[], help="liste à télécharger (répétable)")
    parser.add_argument("--proxy", action="append", default=[], help="proxy direct (répétable)")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--capture", default=str(CAPTURE))
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args(argv)

    lines: list[str] = []
    if args.proxies_file:
        lines.extend(Path(args.proxies_file).read_text(encoding="utf-8", errors="replace").splitlines())
    for url in args.url:
        try:
            lines.extend(fetch_lines(url))
        except Exception as exc:
            print(f"url {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
    lines.extend(args.proxy)

    proxies: list[str] = []
    seen: set[str] = set()
    for line in lines:
        proxy = normalize_proxy(line)
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    proxies = proxies[: max(1, args.limit)]
    if not proxies:
        print("aucun proxy", file=sys.stderr)
        return 2

    capture = load_capture(Path(args.capture))
    if capture is None:
        print(f"capture absente: {args.capture}", file=sys.stderr)
        return 2

    try:
        impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
    except Exception:
        impersonate, fp = "chrome131", None

    tested_egress: set[str] = set()
    lock = threading.Lock()
    results: list[dict] = []

    def test_one(proxy: str) -> dict:
        started = time.time()
        try:
            egress = egress_ip(proxy, max(5.0, min(args.timeout, 12.0)), impersonate, fp)
        except Exception as exc:
            return {"proxy": proxy, "egress": "", "status": "dead", "error": f"{type(exc).__name__}: {exc}"[:200]}
        if not egress:
            return {"proxy": proxy, "egress": "", "status": "dead", "error": "ipify failed"}
        with lock:
            if egress in tested_egress:
                return {"proxy": proxy, "egress": egress, "status": "duplicate"}
            tested_egress.add(egress)
        result = post_once(proxy, args.model, args.timeout, capture=capture, reuse_session=False)
        result["proxy"] = proxy
        result["egress"] = egress
        result["ms"] = round((time.time() - started) * 1000)
        return result

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(test_one, proxy): proxy for proxy in proxies}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    working = [r for r in results if r.get("status") == 200]
    counts: dict[str, int] = {}
    for result in results:
        key = str(result.get("status"))
        counts[key] = counts.get(key, 0) + 1
    summary = {
        "total": len(results),
        "working": len(working),
        "counts": counts,
        "working_ips": sorted({r["egress"] for r in working}),
    }
    Path(args.output).write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    print(f"saved {args.output}")
    return 0 if working else 2


if __name__ == "__main__":
    raise SystemExit(main())
