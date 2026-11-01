"""Chasseur lent d'IP NordVPN fraîche pour les modèles free.

Respecte le throttle AUTH NordVPN : une rotation toutes les ~10 min, attente
des cooldowns, arrêt dès qu'un vrai POST free rend 200. Log dans
``logs/fresh_ip_hunter.log``.

Le proxy n'est pas modifié : dès qu'une IP fraîche existe, son ledger la voit
comme un bucket neuf et l'utilise automatiquement.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\julie\Downloads\opencode-proxy-one")
sys.path.insert(0, str(ROOT))

import requests as http  # noqa: E402
from curl_cffi import requests as cffi  # noqa: E402

import opencode as oc  # noqa: E402
from free.identities import IpIdentity  # noqa: E402

BASE = "http://127.0.0.1:4000"
ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
MODEL = "deepseek-v4.1-flash-free"
INTERVAL_S = 600.0
MAX_ATTEMPTS = 36
LOG_PATH = ROOT / "logs" / "fresh_ip_hunter.log"

with open(ROOT / "free" / "official_payload.json", encoding="utf-8") as f:
    BASE_BODY = json.load(f)["body"]
IMP, FP = oc._get_free_fp_kwargs("1.19.5")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def pool() -> dict:
    return http.get(BASE + "/api/pool-status", timeout=30).json()


def probe(proxy: str):
    body = dict(BASE_BODY)
    body["model"] = MODEL
    body["messages"] = [{"role": "user", "content": "Reply with exactly OK"}]
    body["max_tokens"] = 16
    ident = IpIdentity(
        "hunter",
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
    return cffi.post(
        ENDPOINT,
        headers=headers,
        json=body,
        proxy=proxy,
        timeout=90,
        **({"impersonate": IMP} if IMP else {}),
        **FP,
    )


def pick_station(d: dict) -> dict | None:
    candidates = [
        s
        for s in d.get("stations", [])
        if (s.get("vpn") or {}).get("status") == "connected"
        and bool(s.get("station_usable"))
        and (s.get("vpn") or {}).get("socks5_url")
    ]
    if not candidates:
        return None
    return candidates[int(time.time() // INTERVAL_S) % len(candidates)]


tried: dict[str, int] = {}


def probe_station(st: dict, ip_override: str | None = None) -> bool:
    """Teste réellement l'IP de la station. True seulement sur 2xx."""
    ip = str(ip_override or st.get("current_ip") or "")
    if not ip:
        return False
    if ip in tried:
        log(f"ip {ip} already tested ({tried[ip]})")
        return False
    proxy = str((st.get("vpn") or {}).get("socks5_url") or "").replace("socks5://", "socks5h://")
    if not proxy:
        log(f"no socks url for station {st.get('station')}")
        return False
    log(f"probing {ip} via {proxy}")
    try:
        resp = probe(proxy)
        status = int(resp.status_code)
        tried[ip] = status
        log(
            f"{ip} -> {status} retry-after={resp.headers.get('retry-after')} "
            f"body={(resp.text or '')[:180]!r}"
        )
        if 200 <= status < 300:
            log(f"SUCCESS fresh ip {ip} — free is usable; hunter stops")
            return True
    except Exception as exc:
        tried[ip] = -1
        log(f"probe error on {ip}: {type(exc).__name__}: {exc}")
    return False


log("hunter started")
for attempt in range(1, MAX_ATTEMPTS + 1):
    try:
        d = pool()
    except Exception as exc:
        log(f"pool-status error: {type(exc).__name__}: {exc}")
        time.sleep(60)
        continue

    fresh = [
        s
        for s in d.get("stations", [])
        if s.get("model_usable") and s.get("station_usable") and s.get("current_ip")
    ]
    if fresh:
        log(f"{len(fresh)} station(s) marked fresh by ledger — verifying real 200")
        if any(probe_station(s) for s in fresh):
            sys.exit(0)
        log("fresh ledger entries were false positives (429) — continuing")

    cool = 0.0
    for s in d.get("stations", []):
        v = s.get("vpn") or {}
        cool = max(cool, float(v.get("auth_cool_remaining_s") or 0.0))
        cool = max(cool, float(v.get("auth_global_pause_remaining_s") or 0.0))
    if cool > 0:
        wait = min(cool + 30.0, 900.0)
        log(f"auth cooling {cool:.0f}s — wait {wait:.0f}s")
        time.sleep(wait)
        continue

    st = pick_station(d)
    if st is None:
        log("no connected station — wait 60s")
        time.sleep(60)
        continue

    sid = st.get("station")
    log(f"attempt {attempt}/{MAX_ATTEMPTS}: rotate station {sid} (ip={st.get('current_ip')})")
    try:
        r = http.post(BASE + "/api/vpn/next", json={"station": sid}, timeout=300)
        data = r.json()
    except Exception as exc:
        log(f"rotation error: {type(exc).__name__}: {exc}")
        time.sleep(120)
        continue
    ip = str((data or {}).get("ip") or "")
    if not data.get("ok") or not ip:
        log(f"rotation refused: {json.dumps(data, ensure_ascii=False)[:300]}")
        time.sleep(120)
        continue
    if ip in tried:
        log(f"ip {ip} already tested ({tried[ip]}), wait")
        time.sleep(INTERVAL_S)
        continue

    if probe_station(st, ip):
        sys.exit(0)

    time.sleep(INTERVAL_S)

log("hunter exhausted attempts without a fresh IP")
