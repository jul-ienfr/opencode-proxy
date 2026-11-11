#!/usr/bin/env python3
"""Teste les proxies SOCKS5 dédiés NordVPN avec la capture free 2.2.5.

Les serveurs `socks-*` du catalogue exposent un SOCKS5 sur le port 1080 avec
les credentials de service (`OPENVPN_USER` / `OPENVPN_PASSWORD`). Chaque
serveur a son propre egress, donc on peut tester beaucoup d'IPs sans rebinder
de station Docker.

Aucun credential n'est écrit dans la sortie ni dans le fichier de résultats.

Exemples :
    python scripts/test_socks_pool.py --concurrency 8
    python scripts/test_socks_pool.py --limit 20 --timeout 25
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from curl_cffi import requests  # noqa: E402
from test_free_egress import CAPTURE, load_capture, post_once  # noqa: E402

import opencode as oc  # noqa: E402

CATALOG = ROOT / "logs" / "nordvpn_api.json"
CREDS = ROOT / "credentials.env"
OUT = ROOT / "logs" / "socks_pool_results.json"
IPIFY = "https://api.ipify.org"


def load_credentials() -> tuple[str, str]:
    env: dict[str, str] = {}
    for line in CREDS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    user = env.get("OPENVPN_USER") or env.get("NORDVPN_USER") or ""
    password = env.get("OPENVPN_PASSWORD") or env.get("NORDVPN_PASSWORD") or ""
    if not user or not password:
        raise SystemExit("credentials SOCKS5 absents dans credentials.env")
    return user, password


def load_servers(limit: int, country_filter: str) -> list[dict]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    out: list[dict] = []
    for server in data:
        techs = {str(t.get("identifier")): t for t in (server.get("technologies") or [])}
        socks = techs.get("socks")
        if not socks:
            continue
        pivot = socks.get("pivot") or {}
        if (pivot.get("status") or socks.get("status")) != "online":
            continue
        locations = server.get("locations") or []
        location = locations[0] if locations else {}
        country = ((location.get("country") or {}).get("name") or "") if isinstance(location, dict) else ""
        if country_filter and country.lower() != country_filter.lower():
            continue
        out.append(
            {
                "hostname": str(server.get("hostname") or ""),
                "country": country,
                "load": server.get("load"),
            }
        )
    if limit > 0:
        out = out[:limit]
    return out


def egress_ip(proxy: str, timeout: float, impersonate: str | None, fp: dict | None) -> str:
    kwargs = {"proxies": {"http": proxy, "https": proxy}, "timeout": timeout, "stream": False}
    if impersonate:
        kwargs["impersonate"] = impersonate
    if isinstance(fp, dict):
        kwargs.update(fp)
    response = requests.get(IPIFY, **kwargs)
    text = (response.text or "").strip()
    return text if response.status_code == 200 and text.count(".") == 3 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--country", default="")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--capture", default=str(CAPTURE))
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args(argv)

    capture = load_capture(Path(args.capture))
    if capture is None:
        raise SystemExit(f"capture absente: {args.capture}")
    servers = load_servers(args.limit, args.country)
    if not servers:
        raise SystemExit("aucun serveur SOCKS5 online")
    user, password = load_credentials()
    try:
        impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
    except Exception:
        impersonate, fp = "chrome131", None

    tested_egress: set[str] = set()
    lock = threading.Lock()
    results: list[dict] = []

    def test_one(server: dict) -> dict:
        host = server["hostname"]
        proxy = f"socks5h://{quote(user, safe='')}:{quote(password, safe='')}@{host}:1080"
        started = time.time()
        try:
            egress = egress_ip(proxy, max(5.0, min(args.timeout, 15.0)), impersonate, fp)
        except Exception as exc:
            return {"host": host, "country": server.get("country"), "egress": "", "status": "dead", "error": f"{type(exc).__name__}: {exc}"[:180]}
        if not egress:
            return {"host": host, "country": server.get("country"), "egress": "", "status": "dead", "error": "ipify failed"}
        with lock:
            if egress in tested_egress:
                return {"host": host, "country": server.get("country"), "egress": egress, "status": "duplicate"}
            tested_egress.add(egress)
        result = post_once(proxy, args.model, args.timeout, capture=capture, reuse_session=False)
        result.update({"host": host, "country": server.get("country"), "egress": egress, "ms": round((time.time() - started) * 1000)})
        return result

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(test_one, server): server for server in servers}
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
        "working_ips": sorted({r["egress"] for r in working if r.get("egress")}),
        "working_hosts": sorted({r["host"] for r in working}),
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
