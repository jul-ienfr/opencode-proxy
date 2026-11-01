#!/usr/bin/env python3
"""Chain through NordVPN WG candidates until one returns a real free 200.

This is the runtime counterpart of the discovery scan: on every candidate it
rebinds the station via Compose (hostname + country), waits for a real egress,
probes the official free endpoint, records the result in the ledger and moves
to the next candidate on 429/no-egress.  It stops as soon as one candidate is
200, leaving the station bound to it.

Usage:
    python scripts/fresh_chain.py --station 5
    python scripts/fresh_chain.py --station 6 --start 200
    python scripts/fresh_chain.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
API_URL = "https://api.nordvpn.com/v1/servers?limit=0"
STATE = ROOT / "logs" / "fresh_chain_state.json"
CANDIDATES_CACHE = ROOT / "logs" / "fresh_chain_candidates.json"
API_CACHE = ROOT / "logs" / "nordvpn_api.json"
EMBEDDED = ROOT / "logs" / "gluetun_nordvpn_embedded.json"


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def _fetch_api() -> list[dict]:
    if API_CACHE.exists():
        try:
            raw = json.loads(API_CACHE.read_text(encoding="utf-8"))
            if isinstance(raw, list) and len(raw) > 1000:
                return raw
        except Exception:
            pass
    log("fetching NordVPN API")
    response = requests.get(API_URL, timeout=180)
    response.raise_for_status()
    raw = response.json()
    API_CACHE.parent.mkdir(parents=True, exist_ok=True)
    API_CACHE.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def _registry_hosts() -> list[str]:
    path = ROOT / "logs" / "ip_registry.db"
    if not path.exists():
        return []
    try:
        with sqlite3.connect(path) as conn:
            rows = list(
                conn.execute(
                    "select hostname from ip_state "
                    "where last_status = 200 or state in ('fresh','candidate','assigned') "
                    "order by last_status desc, last_ok_at desc"
                )
            )
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def _active_hosts() -> set[str]:
    try:
        data = requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=20).json()
    except Exception:
        return set()
    hosts = set()
    for station in data.get("stations", []):
        vpn = station.get("vpn") or {}
        for key in ("pinned_hostname", "current_host"):
            value = str(vpn.get(key) or "").strip()
            if value:
                hosts.add(value)
    return hosts


def _is_wireguard(item: dict) -> bool:
    return any(
        isinstance(t, dict) and t.get("identifier") == "wireguard_udp"
        for t in (item.get("technologies") or [])
    )


def build_candidates() -> list[dict]:
    api = _fetch_api()
    embedded = []
    if EMBEDDED.exists():
        try:
            payload = json.loads(EMBEDDED.read_text(encoding="utf-8"))
            embedded = payload.get("servers") or []
        except Exception:
            embedded = []

    active = _active_hosts()
    seen: set[str] = set()

    def add(rows: list[dict], item: dict, source: str) -> None:
        host = str(item.get("hostname") or "").strip()
        if not host or host in seen or host in active:
            return
        country = str(item.get("country") or "").strip()
        if not country:
            loc = (item.get("locations") or [{}])[0]
            if isinstance(loc, dict):
                country = str(((loc.get("country") or {}) if isinstance(loc.get("country"), dict) else {}).get("name") or "")
        if not country:
            return
        seen.add(host)
        rows.append({"host": host, "country": country, "source": source})

    rows: list[dict] = []

    # 1. Hostnames already seen with a 200 in the registry.
    api_by_host = {
        str(item.get("hostname") or ""): item
        for item in api
        if isinstance(item, dict)
    }
    for host in _registry_hosts():
        item = api_by_host.get(host) or {"hostname": host, "country": "Albania"}
        add(rows, item, "registry-200")

    # 2. Active public API WG servers, one per /24, newest first.
    public = []
    for item in api:
        if item.get("status") != "online" or not _is_wireguard(item):
            continue
        station_ip = str(item.get("station") or "")
        loc = (item.get("locations") or [{}])[0]
        country = str(((loc.get("country") or {}) if isinstance(loc, dict) else {}).get("name") or "")
        if not station_ip or not country:
            continue
        public.append((str(item.get("created_at") or ""), station_ip, item))
    public.sort(reverse=True)
    seen_net: set[str] = set()
    for _, station_ip, item in public:
        net = ".".join(station_ip.split(".")[:3])
        if net in seen_net:
            continue
        seen_net.add(net)
        add(rows, item, "public-1-per-24")

    # 3. Old Albanian WG hosts (embedded-only) not currently active.
    for item in embedded:
        host = str(item.get("hostname") or "")
        if item.get("vpn") == "wireguard" and item.get("country") == "Albania" and host.startswith("al"):
            add(rows, item, "albania-legacy")

    # 4. Remaining embedded WireGuard hosts.
    for item in embedded:
        if item.get("vpn") == "wireguard":
            add(rows, item, "embedded-rest")

    # 5. Remaining public servers (multiple per /24).
    for _, _, item in public:
        add(rows, item, "public-rest")

    return rows


def _state_path(station: int) -> Path:
    return ROOT / "logs" / f"fresh_chain_state_{int(station)}.json"


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"index": 0, "results": [], "winner": None}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _ledger_exhausted_ips(api_base: str) -> set[str]:
    try:
        data = requests.get(api_base + "/api/pool-status", timeout=30).json()
    except Exception:
        return set()
    now = time.time()
    out = set()
    for bucket in ((data.get("quota_ledger") or {}).get("buckets") or []):
        if bucket.get("exhausted") and float(bucket.get("reset_at") or 0) > now:
            out.add(str(bucket.get("ip") or ""))
    return out


def mark(api_base: str, ip: str, status: int, retry_after: float, station: int, model: str) -> None:
    body: dict[str, Any] = {
        "ip": ip,
        "model": model,
        "status": status,
        "station": station,
    }
    if status == 429:
        body["retry_after"] = retry_after
    try:
        requests.post(api_base + "/api/free/quota/mark", json=body, timeout=60)
    except Exception:
        pass


async def run(args) -> int:
    from free.ip_registry import ComposeHostnameBinder, StationEgress, StationProbe

    state_path = _state_path(args.station)
    state = load_state(state_path)
    if args.candidates and Path(args.candidates).exists():
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    elif CANDIDATES_CACHE.exists() and not args.refresh:
        candidates = json.loads(CANDIDATES_CACHE.read_text(encoding="utf-8"))
    else:
        log("building candidate list")
        candidates = build_candidates()
        CANDIDATES_CACHE.write_text(json.dumps(candidates), encoding="utf-8")
    if not candidates:
        log("no candidates")
        return 1
    log(f"candidates={len(candidates)} start={args.start or state.get('index', 0)}")

    if args.dry_run:
        for row in candidates[: args.max or 20]:
            print(row["host"], row["country"], row["source"])
        return 0

    egress = StationEgress()
    probe = StationProbe(egress=egress, timeout_s=60.0)
    binder = ComposeHostnameBinder(
        ROOT,
        override_path=f"logs/compose-hostname-{args.station}.yml",
        profile="multi-station",
        settle_s=12.0,
        egress=egress,
        recreate_timeout_s=180.0,
    )
    start = int(args.start if args.start is not None else state.get("index", 0))
    exhausted = _ledger_exhausted_ips(args.api_base)
    tested = 0
    for index in range(start, len(candidates)):
        if args.max and tested >= args.max:
            break
        candidate = candidates[index]
        host = str(candidate.get("host") or "")
        country = str(candidate.get("country") or "")
        if not host:
            continue
        if tested and tested % 20 == 0:
            exhausted = _ledger_exhausted_ips(args.api_base)
            state["index"] = index
            save_state(state_path, state)
        log(f"[{index + 1}/{len(candidates)}] bind {host} ({country})")
        bind = await binder.assign(args.station, host, country=country)
        tested += 1
        if not bind.ok:
            log(f"  bind rejected: {bind.error}")
            state["results"].append({"host": host, "country": country, "status": "bind-error", "error": bind.error})
            state["index"] = index + 1
            save_state(state_path, state)
            continue
        ip = str(bind.egress_ip or "")
        log(f"  egress {ip}")
        if ip and ip in exhausted:
            log("  egress already in the 429 ledger — skip probe")
            state["results"].append({"host": host, "country": country, "ip": ip, "status": "ledger-429"})
            state["index"] = index + 1
            save_state(state_path, state)
            continue
        result = await probe.probe(args.station, args.model)
        log(f"  free status={result.status} ra={result.retry_after_s}")
        state["results"].append(
            {
                "host": host,
                "country": country,
                "ip": ip,
                "status": result.status,
                "retry_after": result.retry_after_s,
            }
        )
        state["index"] = index + 1
        save_state(state_path, state)
        if result.status == 200:
            mark(args.api_base, ip, 200, 0.0, args.station, args.model)
            state["winner"] = {"host": host, "country": country, "ip": ip}
            save_state(state_path, state)
            log(f"WINNER station={args.station} host={host} egress={ip}")
            return 0
        if result.status == 429:
            mark(args.api_base, ip, 429, result.retry_after_s, args.station, args.model)
            exhausted.add(ip)
        # no-egress/probe errors: move on
    log("exhausted without a 200")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=int, default=5)
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--api-base", default="http://127.0.0.1:4000")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--candidates", default="")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
