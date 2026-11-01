#!/usr/bin/env python3
"""Scan NordVPN countries/servers across the VPN stations for a fresh free bucket.

The free quota is bucketed by the upstream-visible IP.  Most NordVPN
datacenter IPs are already exhausted (429 with a long Retry-After), but a
minority still answers 200.  This tool walks the country list on every
station, probes the exact official free body through each station's SOCKS5,
and records the first 200 per station.

It is intentionally a *project* tool (not a throwaway probe):
  * resumable state in ``logs/nordvpn_scan_state.json``
  * append-only log in ``logs/nordvpn_scan.log``
  * one worker per station, country pins via the dashboard API
  * local-DNS fallback (socks5://) when gluetun's DNS is broken

Usage:
    python scripts/nordvpn_scan.py                    # all stations, 1 pass
    python scripts/nordvpn_scan.py --stations 1,2    # two stations
    python scripts/nordvpn_scan.py --passes 3        # repeat the country list
    python scripts/nordvpn_scan.py --countries Albania,Iceland
    python scripts/nordvpn_scan.py --dry-run

A pass = every country once per station.  ``--passes 0`` loops until every
station has a winner or the process is stopped (use for an exhaustive hunt).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

import opencode as oc  # noqa: E402
from free.identities import IpIdentity  # noqa: E402

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
MODEL = "deepseek-v4.1-flash-free"
STATE_PATH = ROOT / "logs" / "nordvpn_scan_state.json"
LOG_PATH = ROOT / "logs" / "nordvpn_scan.log"
LOCK = threading.Lock()

FALLBACK_COUNTRIES = [
    "Albania",
    "Bosnia and Herzegovina",
    "Iceland",
    "Latvia",
    "Estonia",
    "Lithuania",
    "Bulgaria",
    "Serbia",
    "North Macedonia",
    "Moldova",
    "Georgia",
    "Armenia",
    "Kazakhstan",
    "Vietnam",
    "Thailand",
    "Malaysia",
    "Indonesia",
    "Philippines",
    "Chile",
    "Colombia",
    "Peru",
    "Argentina",
    "Costa Rica",
    "Panama",
    "Uruguay",
    "Romania",
    "Poland",
    "Czech Republic",
    "Hungary",
    "Slovakia",
    "Slovenia",
    "Croatia",
    "Greece",
    "Portugal",
    "Ireland",
    "Finland",
    "Norway",
    "Denmark",
    "Austria",
    "Belgium",
    "Luxembourg",
    "Ukraine",
    "Turkey",
    "Israel",
    "South Africa",
    "India",
    "Singapore",
    "Japan",
    "South Korea",
    "Australia",
    "New Zealand",
    "Mexico",
    "Brazil",
    "Canada",
    "United States",
    "United Kingdom",
    "Germany",
    "Netherlands",
    "France",
    "Sweden",
    "Switzerland",
]


def _log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
    with LOCK:
        print(line, flush=True)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    state.setdefault("results", [])
    state.setdefault("stations", {})
    return state


def _save_state(state: dict) -> None:
    with LOCK:
        try:
            tmp = STATE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            tmp.replace(STATE_PATH)
        except Exception as exc:
            _log(f"state save failed: {type(exc).__name__}: {exc}")


def _api(base: str, path: str, payload: dict, timeout: float = 120.0):
    try:
        return requests.post(base + path, json=payload, timeout=timeout)
    except Exception as exc:
        _log(f"API {path} failed: {type(exc).__name__}: {exc}")
        return None


def _countries_from_api() -> tuple[list[str], Counter]:
    try:
        with requests.get(
            "https://api.nordvpn.com/v1/servers?limit=0", timeout=120
        ) as resp:
            data = resp.json()
        counts: Counter = Counter()
        for server in data:
            if str(server.get("status")) != "online":
                continue
            locations = server.get("locations") or [{}]
            name = (locations[0].get("country") or {}).get("name")
            if name:
                counts[str(name)] += 1
        if counts:
            # obscure countries first: fewer servers / less likely burned
            ordered = sorted(counts, key=lambda c: (counts[c], c))
            return ordered, counts
    except Exception as exc:
        _log(f"country list fetch failed: {type(exc).__name__}: {exc}")
    return list(FALLBACK_COUNTRIES), Counter(dict.fromkeys(FALLBACK_COUNTRIES, 0))


def _egress(port: int, local_dns: bool) -> str:
    scheme = "socks5" if local_dns else "socks5h"
    proxy = f"{scheme}://127.0.0.1:{port}"
    try:
        return curl_requests.get(
            "https://api.ipify.org",
            proxies={"http": proxy, "https": proxy},
            timeout=12,
            impersonate="chrome",
        ).text.strip()
    except Exception:
        return ""


def _probe(port: int, ip: str, body: dict, imp: str, fp: dict | None):
    ident = IpIdentity(
        f"scan-{port}-{ip}",
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
    last_error = ""
    for local_dns in (False, True):
        scheme = "socks5" if local_dns else "socks5h"
        proxy = f"{scheme}://127.0.0.1:{port}"
        proxies = {"http": proxy, "https": proxy}
        try:
            resp = curl_requests.post(
                ENDPOINT,
                headers=headers,
                json=body,
                proxies=proxies,
                timeout=60,
                stream=True,
                **({"impersonate": imp} if imp else {}),
                **(fp or {}),
            )
            status = resp.status_code
            retry_after = resp.headers.get("retry-after") or ""
            try:
                resp.close()
            except Exception:
                pass
            return status, retry_after, "local" if local_dns else "remote"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return 0, last_error[:160], "none"


def _pin(base: str, station: int, country: str) -> bool:
    resp = _api(
        base,
        f"/api/vpn/station/{station}/pin_country",
        {"country": country},
        timeout=120,
    )
    if resp is None or resp.status_code != 200:
        _log(
            f"s{station} pin {country!r} refused: "
            f"{getattr(resp, 'status_code', '?')} {getattr(resp, 'text', '')[:120]}"
        )
        return False
    return True


def _wait_egress(port: int, previous: str, timeout: float = 45.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for local_dns in (False, True):
            ip = _egress(port, local_dns)
            if ip and ip != previous:
                return ip
        time.sleep(2)
    for local_dns in (False, True):
        ip = _egress(port, local_dns)
        if ip:
            return ip
    return ""


def _scan_station(
    base: str,
    station: int,
    port: int,
    countries: list[str],
    passes: int,
    body: dict,
    imp: str,
    fp: dict | None,
    state: dict,
    max_attempts: int,
) -> None:
    key = str(station)
    with LOCK:
        station_state = state["stations"].setdefault(
            key, {"winner": None, "attempts": 0, "last_country": None}
        )
    if station_state.get("winner"):
        _log(f"s{station} already has winner {station_state['winner']}")
        return

    attempts = 0
    pass_index = 0
    while passes == 0 or pass_index < passes:
        pass_index += 1
        for country in countries:
            if station_state.get("winner"):
                return
            if max_attempts and attempts >= max_attempts:
                _log(f"s{station} max attempts {max_attempts} reached")
                return
            attempts += 1
            previous = _egress(port, False) or _egress(port, True)
            _log(f"s{station} pass={pass_index} attempt={attempts} pin {country!r}")
            if not _pin(base, station, country):
                continue
            ip = _wait_egress(port, previous)
            if not ip:
                _log(f"s{station} {country!r}: no egress after pin")
                continue
            status, retry_after, dns = _probe(port, ip, body, imp, fp)
            result = {
                "station": station,
                "country": country,
                "ip": ip,
                "status": status,
                "retry_after": retry_after,
                "dns": dns,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with LOCK:
                state["results"].append(result)
                station_state["attempts"] = attempts
                station_state["last_country"] = country
                if status == 200:
                    station_state["winner"] = result
            _save_state(state)
            _log(
                f"s{station} {country!r} ip={ip} free={status} "
                f"retry_after={retry_after or '-'} dns={dns}"
            )
            if status == 200:
                _api(base, "/api/vpn/health-check", {})
                _api(base, "/api/free/quota/clear", {"ip": ip})
                _api(base, "/api/free/quota/clear", {"model": MODEL})
                _log(f"s{station} WINNER ip={ip} country={country!r}")
                return
        if passes == 0:
            _log(f"s{station} pass={pass_index} complete, continuing (passes=0)")
    _log(f"s{station} finished {attempts} attempts without a 200")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stations", default="1,2,3,4,5,6,7")
    parser.add_argument("--countries", default="")
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stations = [int(x) for x in args.stations.split(",") if x.strip().isdigit()]
    if not stations:
        print("no station selected")
        return 2

    base = args.proxy_url.rstrip("/")
    api_countries, counts = _countries_from_api()
    if args.countries:
        countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    else:
        countries = api_countries

    with open(ROOT / "free" / "official_payload.json", encoding="utf-8") as f:
        body = json.load(f)["body"]
    body["model"] = MODEL
    imp, fp = oc._get_free_fp_kwargs("1.19.5")

    print(f"stations={stations} countries={len(countries)} passes={args.passes}")
    print(f"first countries: {countries[:10]}")
    if args.dry_run:
        for station in stations:
            print(f"  s{station} -> 127.0.0.1:{1079 + station}")
        return 0

    state = _load_state()
    _api(base, "/api/vpn/rotation-paused", {"paused": True})
    _api(base, "/api/vpn-config", {"egress_allow_any_country": True})
    _save_state(state)

    workers = min(len(stations), 7)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _scan_station,
                base,
                station,
                1079 + station,
                countries,
                args.passes,
                body,
                imp,
                fp,
                state,
                args.max_attempts,
            )
            for station in stations
        ]
        for fut in futures:
            fut.result()

    _save_state(state)
    winners = {
        s: v.get("winner") for s, v in state.get("stations", {}).items() if v.get("winner")
    }
    print(f"winners: {json.dumps(winners, ensure_ascii=False)}")
    return 0 if len(winners) == len(stations) else 1


if __name__ == "__main__":
    raise SystemExit(main())
