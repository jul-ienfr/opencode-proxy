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

The count is only a search budget for stations that have **not** found a
working IP yet: the first 200 freezes that station (sticky winner), and the
scanner never recreates or re-pins it again.  The remaining attempts are
not applied to a working station.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
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
IP_URLS = [
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ipinfo.io/ip",
]
STATE_PATH = ROOT / "logs" / "nordvpn_scan_state.json"
LOG_PATH = ROOT / "logs" / "nordvpn_scan.log"
LOCK = threading.Lock()
#: station -> IP, one IP per station unless --allow-shared-ips is set.
CLAIMED_IPS: dict[str, int] = {}

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


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
        return True
    except ValueError:
        return False


def _egress(port: int, local_dns: bool) -> str:
    """Probe the public IP through one station with several IP services.

    Remote DNS (socks5h) lets gluetun resolve the hostname; local DNS
    (socks5) bypasses a broken gluetun resolver.  Every service is tried
    with both modes; the first valid IP wins.
    """
    scheme = "socks5" if local_dns else "socks5h"
    proxy = f"{scheme}://127.0.0.1:{port}"
    proxies = {"http": proxy, "https": proxy}
    for url in IP_URLS:
        try:
            text = curl_requests.get(
                url,
                proxies=proxies,
                timeout=5,
                impersonate="chrome",
            ).text.strip()
        except Exception:
            continue
        if _valid_ip(text):
            return text
    return ""


def _control_egress(station: int) -> str:
    """Last-resort detection: gluetun's own public-IP endpoint."""
    container = "opencode-vpn" if station == 1 else f"opencode-vpn-{station}"
    script = (
        'wget -q -O - -T 10 --header="X-API-Key: $VPN_CONTROL_API_KEY" '
        "http://127.0.0.1:8000/v1/publicip/ip"
    )
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        data = json.loads((proc.stdout or "{}").strip() or "{}")
        ip = str(data.get("public_ip") or "").strip()
        return ip if _valid_ip(ip) else ""
    except Exception:
        return ""


def _detect_egress(station: int, port: int) -> tuple[str, str]:
    """(ip, source): remote DNS → local DNS → control server."""
    for local_dns, source in ((False, "remote"), (True, "local")):
        ip = _egress(port, local_dns)
        if ip:
            return ip, source
    ip = _control_egress(station)
    if ip:
        return ip, "control"
    return "", "none"


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


def _recreate(station: int) -> bool:
    """Recreate one gluetun container to draw a new server from the env country."""
    service = "vpn-gluetun" if station == 1 else f"vpn-gluetun-{station}"
    try:
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "multi-station",
                "up",
                "-d",
                "--force-recreate",
                "--no-deps",
                service,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            _log(
                f"s{station} recreate {service} rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:160]}"
            )
            return False
        return True
    except Exception as exc:
        _log(f"s{station} recreate failed: {type(exc).__name__}: {exc}")
        return False


def _wait_egress(station: int, port: int, previous: str, timeout: float = 45.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip, _source = _detect_egress(station, port)
        if ip and ip != previous:
            return ip
        time.sleep(2)
    ip, _source = _detect_egress(station, port)
    return ip


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
    recreate: bool,
    allow_shared: bool,
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
            previous = "" if recreate else _detect_egress(station, port)[0]
            if recreate:
                _log(f"s{station} pass={pass_index} attempt={attempts} recreate")
                if not _recreate(station):
                    continue
                time.sleep(10)
                ip = _wait_egress(station, port, "")
            else:
                _log(f"s{station} pass={pass_index} attempt={attempts} pin {country!r}")
                if not _pin(base, station, country):
                    continue
                ip = _wait_egress(station, port, previous)
            if not ip:
                _log(
                    f"s{station} {country!r}: no egress after "
                    f"{'recreate' if recreate else 'pin'}"
                )
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
            duplicate = False
            owner = None
            if status == 200:
                # One distinct IP per station: a duplicate is a shared bucket,
                # not a second functional station.
                with LOCK:
                    owner = CLAIMED_IPS.get(ip)
                    if not allow_shared and owner is not None and owner != station:
                        duplicate = True
                    else:
                        CLAIMED_IPS[ip] = station
                if duplicate:
                    result["duplicate_of"] = owner
                    _log(
                        f"s{station} duplicate IP {ip} already claimed by s{owner} "
                        "— continuing"
                    )
                else:
                    ip2, _src = _detect_egress(station, port)
                    if ip2 and ip2 != ip:
                        with LOCK:
                            if CLAIMED_IPS.get(ip) == station:
                                CLAIMED_IPS.pop(ip, None)
                        _log(
                            f"s{station} IP changed after 200 ({ip} -> {ip2}) "
                            "— continuing"
                        )
                        duplicate = True
            with LOCK:
                state["results"].append(result)
                station_state["attempts"] = attempts
                station_state["last_country"] = country
                if status == 200 and not duplicate:
                    station_state["winner"] = result
            _save_state(state)
            _log(
                f"s{station} {country!r} ip={ip} free={status} "
                f"retry_after={retry_after or '-'} dns={dns}"
            )
            if status == 200 and not duplicate:
                _api(base, "/api/vpn/health-check", {})
                _api(base, "/api/free/quota/clear", {"ip": ip})
                _api(base, "/api/free/quota/clear", {"model": MODEL})
                _log(f"s{station} WINNER ip={ip} country={country!r}")
                return
        if passes == 0:
            _log(f"s{station} pass={pass_index} complete, continuing (passes=0)")
    _log(f"s{station} finished {attempts} attempts without a 200")


def _normalize_claims(state: dict, allow_shared: bool) -> dict[str, int]:
    """Keep one winner per IP; clear duplicate winners so they search again."""
    winners = []
    for sid, st in state.get("stations", {}).items():
        winner = st.get("winner")
        if isinstance(winner, dict) and winner.get("ip"):
            winners.append((str(winner.get("at") or ""), int(sid), winner))
    winners.sort()
    claimed: dict[str, int] = {}
    for _at, sid, winner in winners:
        ip = str(winner["ip"])
        if not allow_shared and ip in claimed:
            state["stations"][str(sid)]["winner"] = None
            _log(f"reset duplicate winner s{sid} ip={ip} (kept by s{claimed[ip]})")
        else:
            claimed[ip] = sid
    return claimed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stations", default="1,2,3,4,5,6,7")
    parser.add_argument("--countries", default="")
    parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help=(
            "max full country passes per station; with one country this is the "
            "max number of server draws. A station stops immediately on its "
            "first 200 — the limit only applies to stations still searching."
        ),
    )
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--allow-shared-ips", action="store_true")
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
    CLAIMED_IPS.clear()
    CLAIMED_IPS.update(_normalize_claims(state, args.allow_shared_ips))
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
                args.recreate,
                args.allow_shared_ips,
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
